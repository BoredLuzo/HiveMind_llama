from __future__ import annotations
import asyncio, hashlib, json, logging, math, os, time
from pathlib import Path
import httpx

logger = logging.getLogger("hivemind.duo")

from context.chat import _load_chat_context, _chat_context_valid, _mutate_chat_context, _save_chat_context, _SESSIONS_DIR
from context.resume import _try_resume
from tools.definitions import _get_inline_tools, _filter_tools_for_mode
from explore.cache import (
    _pre_explore_cache, _explore_cache_key, _explore_cache_valid,
    _pre_explore_cache_set, _get_pre_explore_lock,
    _PRE_EXPLORE_CACHE_TTL, _explore_extract_files,
)
from hive_functions.tree_scout import get_workspace_tree, partition_tree_async, parse_contract_summary, select_analysis_window_async
from hive_functions.prompts import EXPLORE_CODEBASE_PROMPT
from core.duo._utils import _merge_down, _split_paths_by_parent
from core.duo._portcheck import _p2_alive, _port_alive_with_retry
from core.duo_helpers import _RE_FILE_EXT, _inject_no_think_directive  # noqa: F401 (_RE_FILE_EXT genutzt unten)
from core.duo_helpers import _run_parallel_pre_explore
from hive_functions.pre_explore import run_pre_explore


# ── Analyse-Fenster-Memo (2026-08-17) ──────────────────────────────────────────
# PageRank-selektiertes Fenster (voller Walk + regex Import-Graph + PageRank →
_WINDOW_MEMO: dict[str, tuple[float, list[str]]] = {}
_WINDOW_MEMO_TTL = 30.0
_WINDOW_MEMO_MAX = 8


async def _get_analysis_window(
    ws: str,
    max_files: int,
    max_depth: int,
) -> list[str]:
    if not ws:
        return []
    _key = f"{ws}|{max_files}|{max_depth}"
    _now = time.time()
    _ent = _WINDOW_MEMO.get(_key)
    if _ent and (_now - _ent[0]) <= _WINDOW_MEMO_TTL:
        return _ent[1]
    _win = await select_analysis_window_async(
        ws, max_files=max(max_files, 1), max_depth=max_depth,
    )
    _WINDOW_MEMO[_key] = (_now, _win)
    if len(_WINDOW_MEMO) > _WINDOW_MEMO_MAX:
        _oldest = min(_WINDOW_MEMO, key=lambda k: _WINDOW_MEMO[k][0])
        _WINDOW_MEMO.pop(_oldest, None)
    return _win


def _window_params(ctx) -> tuple[int, int]:
    return (
        int(ctx.settings.get("duo_tree_scout_max_files", 200)),
        int(ctx.settings.get("duo_tree_scout_max_depth", 4)),
    )


def _partitions_from_window(window_paths: list[str], effective_max_files: int) -> list[dict]:


    _by_top: dict[str, list[str]] = {}
    for _wp in window_paths or []:
        _p0 = _wp.split("/")[0] if "/" in _wp else "__root__"
        _by_top.setdefault(_p0, []).append(_wp)
    out: list[dict] = []
    for _top_lbl in sorted(_by_top, key=lambda k: (-len(_by_top[k]), k)):
        _grp = _by_top[_top_lbl]
        for _ci in range(0, max(1, len(_grp)), max(1, effective_max_files)):
            _chunk = _grp[_ci:_ci + effective_max_files]
            _lbl = _top_lbl + (f":sz{_ci//effective_max_files+1}" if len(_grp) > effective_max_files else "")
            out.append({"paths": _chunk, "label": _lbl})
    return out


async def _rebuild_tree_ctx_from_window(
    tree_ctx: str,
    ws: str,
    window: list[str],
    max_depth: int,
) -> str:


    from hive_functions.tree_scout import build_tree_from_paths
    if not window or not tree_ctx:
        return tree_ctx
    body = build_tree_from_paths(ws, window, max_depth=max_depth)
    if not body:
        return tree_ctx
    body_lines = body.splitlines()
    marker = body_lines[0] if body_lines else ""
    idx = tree_ctx.find(marker) if marker else -1
    if idx < 0:
        _m = re.search(r"\n?📁", tree_ctx)
        idx = _m.start() if _m else len(tree_ctx)
    header = tree_ctx[:idx].rstrip()
    return (header + "\n" + body) if header else body


def _validate_explore_plan(
    explore_ctx: str,
    touched_partitions: list[str],
    min_steps: int = 1,
) -> tuple[bool, str]:
    if not explore_ctx or not explore_ctx.strip():
        return False, "explore_ctx is empty"
    if explore_ctx.lstrip().startswith("["):
        return False, f"explore_ctx contains error token: {explore_ctx[:60]}"
    import re as _re_vp
    _action_tokens = _re_vp.findall(
        r'\b(write|edit|create|implement|add|modify|update|fix|patch'
        r'|set up|configure|install|connect|integrate|change'
        r'|include|use|run|build|deploy|define|init|setup|remove'
        r'|refactor|replace|extract|move|rename|convert|generate)\b',
        explore_ctx,
        _re_vp.IGNORECASE,
    )
    if len(_action_tokens) < min_steps:
        return False, (
            f"plan has {len(_action_tokens)} action tokens, "
            f"expected >= {min_steps}"
        )
    _missing = []
    for _partition_label in (touched_partitions or []):
        if _partition_label and _partition_label not in explore_ctx:
            _missing.append(_partition_label)
    if _missing and len(_missing) == len(touched_partitions):
        return False, f"no touched partitions referenced in plan: {_missing[:5]}"
    return True, "ok"


async def _phase_pre_explore_bootstrap(ctx, state: dict):
    """P1: Entry-Reads, Inits, Resume-Check, Workspace/Tree-Scout, Follow-up-
    Hint, Static-Repo-Map-only — aus _phase_pre_explore extrahiert (mechanisch).
    Produkte landen in state; der Parent liest sie zurueck."""
    # AGENTIC-FIX (2026-09-02): _phase_pre_explore_bootstrap was extracted from
    # _phase_pre_explore but lost the local `exec_mdl`. Every bare `exec_mdl`
    # reference below (NameError: name 'exec_mdl' is not defined) crashed every
    # code_duo run. Read it from state (set by _phase_vram) like the parent does.
    exec_mdl = state.get("exec_mdl", "") or ""
    # PHASE-FIX (2026-09-02): same extraction loss — tree-scout limits live in
    # state (set by _phase_vram); provide defaults so the tree scout never raises.
    _MAX_TREE_DEPTH = int(state.get("_MAX_TREE_DEPTH") or 4)
    _MAX_TREE_FILES = int(state.get("_MAX_TREE_FILES") or 200)
    _explore_ctx: str = ""
    _pre_explore_msgs: list = []
    _tree_ctx: str = ""
    _plan_tracker = None
    _workers_were_loaded = False # PRE-EXPLORE-GATE-FIX
    _use_parallel = False        # PRE-EXPLORE-GATE-FIX
    _worker_slots: list = []     # PRE-EXPLORE-GATE-FIX
    _xexplore_mdl = exec_mdl    # PRE-EXPLORE-GATE-FIX
    n_parallel = 1              # PRE-EXPLORE-GATE-FIX
    _partitions = None
    _xtask: asyncio.Task | None = None
    _touched_paths: set[str] = set()
    _contracts_raw: list = []    # PRE-EXPLORE-GATE-FIX
    _effective_max_files = 10
    _static_map_task: asyncio.Task | None = None

    # THINKING-OVERRIDE: Expliziter UI-Toggle (duo_coder_tool_thinking) gewinnt
    _x_thinking_override: bool | None = (
        ctx.duo_config.coder_tool_thinking
        if ctx.duo_config.coder_tool_thinking_explicit
        else None
    )

    # ── Resume check: continue a previous interrupted run ──────────────────
    _resume_data: dict | None = None
    if ctx.chat_id:
        _resume_data, _resume_sse = await _try_resume(ctx.chat_id, ctx.emit)
        for _r_sse in _resume_sse:
            yield _r_sse
    if _resume_data:
        _resume_block    = _resume_data["resume"]
        _explore_ctx     = _resume_data.get("explore_ctx", "")
        _ws_str          = _resume_data.get("workspace", "") or str(Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve())
        _pre_explore_msgs = []

    _chat_ctx_loaded: dict = _load_chat_context(ctx.chat_id) if ctx.chat_id else {}
    _chat_ctx_valid:  bool = _chat_context_valid(_chat_ctx_loaded)

    if not _resume_data:
        # WORKSPACE-REWORK (2026-08-25): Central resolution via
        # utils/workspace_resolve.py — identical chain as server.py/duo_runner.
        from utils.workspace_resolve import resolve_workspace as _ws_resolve_fn
        _ws_str, _ws_src = _ws_resolve_fn(ctx.settings or {}, _chat_ctx_loaded, ctx.user_input)
        logger.warning("[WS-RESOLVE] pre-explore workspace=%s (source=%s)", _ws_str, _ws_src)

    if _chat_ctx_valid and not _explore_ctx and not ctx.duo_config.pre_explore:
        _explore_ctx      = _chat_ctx_loaded.get("explore_ctx", "")
        _pre_explore_msgs = []
        # Restore touched_paths from previous run in same session —
        # only if the task matches (same task_sig), otherwise start clean.
        _task_sig_current = hashlib.md5(ctx.user_input[:60].encode()).hexdigest()[:8]
        _task_sig_persisted = _chat_ctx_loaded.get("task_sig", "")
        if not _touched_paths and _chat_ctx_loaded and _task_sig_persisted == _task_sig_current:
            _touched_paths = set(_chat_ctx_loaded.get("touched_paths", []))
        if _explore_ctx:
            yield await ctx.emit({"type": "status",
                "content": f"⚡ Chat context loaded — workspace: {_ws_str} (no re-explore needed)"})
            if not ctx.duo_config.pre_explore:
                _ck_ctx = _explore_cache_key(_ws_str, exec_mdl, ctx.user_input)
                if not _explore_cache_valid(_pre_explore_cache.get(_ck_ctx)):
                    _file_snap_ctx = _chat_ctx_loaded.get("files", {})
                    await _pre_explore_cache_set(_ck_ctx, {
                        "ctx":           _explore_ctx,
                        "msgs":          [],
                        "files":         _file_snap_ctx,
                        "ts":            _chat_ctx_loaded.get("ts", time.time()),
                        "workspace":     str(Path(_ws_str).resolve()),  # BUG-2 FIX
                        "from_chat_ctx": True,
                    })
            else:
                # ctx.duo_config.pre_explore=True: discard explore_ctx from
                # persistence — Pre-Explore will run fresh.
                _explore_ctx = ""

    _follow_up_hint = ""
    _follow_up_task_ctx = ""
    try:
        _chat_ctx_loaded = _chat_ctx_loaded or {}
        _last_run = _chat_ctx_loaded.get("last_run") or {}
        _cur_sig = hashlib.md5(ctx.user_input[:60].encode()).hexdigest()[:8]
        _prev_sig = _chat_ctx_loaded.get("task_sig", "")
        _is_follow_up = bool(
            _last_run.get("task")
            and _last_run.get("at")
            and _cur_sig != _prev_sig
        )
        logger.warning("[FOLLOW-UP-HINT] chat=%s last_run=%s sig_cur=%s sig_prev=%s is_follow_up=%s",
                       ctx.chat_id, bool(_last_run.get("task")), _cur_sig, _prev_sig, _is_follow_up)
        if _is_follow_up:
            from hive_functions.ctx_utils import compute_char_caps
            _fup_caps = compute_char_caps(
                int(state.get("_exec_ctx") or 8192),
                overrides=ctx.settings.get("duo_caps"),
            )
            _last_task_txt = str(_last_run.get("task") or "")[:_fup_caps.task]
            _last_plan_txt = str(_last_run.get("plan") or "")[:_fup_caps.plan]
            _parts = ["[Previous run in this chat — context for this follow-up question]"]
            _parts.append(f"Task: {_last_task_txt}")
            _wfs = _last_run.get("written_files") or []
            if _wfs:
                _parts.append("Written/changed files:\n" + "\n".join(f"  - {f}" for f in _wfs[:20]))
            if _last_plan_txt:
                _parts.append(f"Plan (truncated): {_last_plan_txt}")
            _parts.append(f"Status: {_last_run.get('stop_reason', '?')}")
            _parts.append(
                "Use these files as a starting point. Re-read them if needed — "
                "they may have changed since the last run.\n[End previous run]"
            )
            _follow_up_hint = "\n\n".join(_parts)
            _follow_up_task_ctx = (
                f"Task: {_last_task_txt}\n"
                f"Plan (truncated): {_last_plan_txt}"
            )
            yield await ctx.emit({"type": "status",
                "content": "🔁 Previous run loaded — follow-up context (files/plan) injected"})
    except Exception as _fue:
        logger.warning("[FOLLOW-UP-HINT] Error building hint: %s", _fue)
        _follow_up_hint = ""
        _follow_up_task_ctx = ""
    state["_follow_up_hint"] = _follow_up_hint
    state["_follow_up_task_ctx"] = _follow_up_task_ctx
    _parallel_explore_ran = False
    #
    _duo_pre_explore_user_explicit: bool = bool(ctx.duo_config.pre_explore)
    if ctx.duo_config.pre_explore and (ctx.duo_config.chunking or ctx.duo_config.planner) and not _explore_ctx and not _resume_data:
        yield await ctx.emit({"type": "status",
            "content": "🔍 Pre-Explore active — planner uses workspace context"})
    logger.warning(
        "[PRE-EXPLORE-DIAG] ctx.duo_config.pre_explore=%s ctx.duo_config.chunking=%s ctx.duo_config.planner=%s "
        "ctx.duo_config.parallel_preexplore=%s _worker_count=%d "
        "_explore_ctx_len=%d _resume_data=%s _ws_str=%r",
        ctx.duo_config.pre_explore, ctx.duo_config.chunking, ctx.duo_config.planner,
        ctx.duo_config.parallel_preexplore,
        len((ctx.settings.get("exploration_agent") or {}).get("workers") or []),
        len(_explore_ctx) if _explore_ctx else 0,
        bool(_resume_data),
        (_ws_str or "")[:60],
    )
    if not _tree_ctx and _ws_str:
        try:
            _tree_ctx = await get_workspace_tree(
                task      = ctx.user_input,
                ws_str    = _ws_str,
                client    = None,
                port      = 0,
                model     = "",
                max_depth = int(ctx.settings.get("duo_tree_scout_max_depth", _MAX_TREE_DEPTH)),
                max_files = int(ctx.settings.get("duo_tree_scout_max_files", _MAX_TREE_FILES)),
                enabled   = bool(ctx.settings.get("duo_tree_scout_enabled", True)),
            )
        except Exception:
            _tree_ctx = ""
    if not _explore_ctx and _ws_str and not _resume_data:
        try:
            _ws = Path(_ws_str)
            if _ws.is_dir() and not any(True for _ in _ws.rglob("*") if _.is_file()):
                _explore_ctx = (
                    "## EMPTY WORKSPACE — No existing codebase\n\n"
                    "This is a greenfield project with no prior code. "
                    "Plan the architecture from scratch based on the task. "
                    "No existing files, no legacy constraints."
                )
                _tree_ctx = ""
                yield await ctx.emit({"type": "status",
                    "content": "📭 Empty workspace — pre-explore skipped"})
        except Exception:
            pass
    if not ctx.duo_config.pre_explore and not _explore_ctx and not _resume_data and _tree_ctx and _ws_str:
        try:
            _wmax, _wdepth = _window_params(ctx)
            _sm_window = await _get_analysis_window(_ws_str, _wmax, _wdepth)
            _tree_ctx = await _rebuild_tree_ctx_from_window(_tree_ctx, _ws_str, _sm_window, _wdepth)
            _sm_parts = await partition_tree_async(
                _tree_ctx,
                max_files_per_partition=int(ctx.settings.get("duo_partition_max_files", 30)),
                workspace_root=_ws_str,
                preselect_paths=_sm_window,
            )
            if _sm_parts:
                from hive_functions.static_repomap import build_static_repomap
                from hive_functions.ctx_utils import derive_static_map_budget
                _sm_budget = derive_static_map_budget(
                    state.get("_exec_ctx"),
                    ctx.settings.get("duo_planner_ctx_target", 0),
                    ctx.settings.get("duo_static_map_chars", 0),
                )
                _sm_result = await build_static_repomap(_ws_str, _sm_parts, char_budget=_sm_budget)
                if _sm_result:
                    _explore_ctx = _sm_result
                    logger.info("[STATIC-REPO-MAP-ONLY] %d chars, %d partitions (LLM pre-explore disabled)", len(_sm_result), len(_sm_parts))
                    yield await ctx.emit({"type": "status", "content": "📋 Static Repo-Map created (LLM Pre-Explore disabled)"})
        except Exception as _smo_err:
            logger.warning("[STATIC-REPO-MAP-ONLY] failed: %s", _smo_err)
    state.update({
        "_ws_str": _ws_str,
        "_explore_ctx": _explore_ctx,
        "_tree_ctx": _tree_ctx,
        "_resume_data": _resume_data,
        "_pre_explore_msgs": _pre_explore_msgs,
        "_touched_paths": _touched_paths,
        "_chat_ctx_loaded": _chat_ctx_loaded,
        "_x_thinking_override": _x_thinking_override,
        "_parallel_explore_ran": _parallel_explore_ran,
        "_duo_pre_explore_user_explicit": _duo_pre_explore_user_explicit,
        "_contracts_raw": _contracts_raw,
        "_plan_tracker": _plan_tracker,
        "_worker_slots": _worker_slots,
        "_workers_were_loaded": _workers_were_loaded,
        "_use_parallel": _use_parallel,
        "_xexplore_mdl": _xexplore_mdl,
        "_partitions": _partitions,
        "_effective_max_files": _effective_max_files,
        "_static_map_task": _static_map_task,
        "_xtask": _xtask,
    })

async def _phase_pre_explore_cache(ctx, state: dict):
    """P2: Pre-Explore-Cache-Hit (own Guard) — aus _phase_pre_explore extrahiert."""
    # PHASE-FIX (2026-09-02): this phase was extracted from _phase_pre_explore but
    # lost its locals. Unpack them from state (like _phase_pre_explore_finalize
    # does) so the function no longer raises UnboundLocalError when the
    # `pre_explore` branch is skipped (e.g. pre_explore=False).
    exec_mdl = state.get("exec_mdl", "") or ""
    _ws_str = state.get("_ws_str", "") or ""
    _resume_data = state.get("_resume_data")
    _explore_ctx = state.get("_explore_ctx", "") or ""
    _pre_explore_msgs = list(state.get("_pre_explore_msgs") or [])
    _contracts_raw = state.get("_contracts_raw") or []
    _plan_tracker = state.get("_plan_tracker")
    _touched_paths = state.get("_touched_paths") or set()
    _ck = state.get("_ck", "")
    _xexplore_mdl_early = state.get("_xexplore_mdl_early") or exec_mdl
    if ctx.duo_config.pre_explore:
        #
        _xexplore_cfg_early = ctx.settings.get("exploration_agent") or {}
        _xexplore_mdl_early = exec_mdl
        if _xexplore_cfg_early.get("enabled"):
            _xcfg_model_early = (_xexplore_cfg_early.get("model") or "").strip()
            _xcfg_workers_early = _xexplore_cfg_early.get("workers") or []
            _xcfg_first_early = next(
                ((w.get("model") or "").strip() for w in _xcfg_workers_early if (w.get("model") or "").strip()),
                None
            )
            if ctx.duo_config.parallel_preexplore and _xcfg_first_early:
                _xexplore_mdl_early = _xcfg_first_early
            elif _xcfg_model_early:
                _xexplore_mdl_early = _xcfg_model_early
            elif _xcfg_first_early:
                _xexplore_mdl_early = _xcfg_first_early
        _ck = _explore_cache_key(_ws_str, _xexplore_mdl_early, ctx.user_input)
        _cached_entry = _pre_explore_cache.get(_ck)
        # PRE-EXPLORE-CACHE-FIX: actually evaluate the cache hit. The entry
        # was written before, but never read again — every run paid the full
        # LLM cost. With unchanged workspace + identical task/settings
        # signature, the stored explore_ctx is reused.
        if (
            ctx.duo_config.pre_explore and not _resume_data and not _explore_ctx
            and _cached_entry is not None and _explore_cache_valid(_cached_entry)
        ):
            _cached_ctx = str(_cached_entry.get("ctx") or "").strip()
            if len(_cached_ctx) >= 30:
                _explore_ctx = _cached_ctx
                _pre_explore_msgs = list(_cached_entry.get("msgs") or [])
                logger.info(
                    "[PRE-EXPLORE-CACHE-HIT] reuse explore_ctx (%d chars), msgs=%d",
                    len(_explore_ctx), len(_pre_explore_msgs),
                )
                yield await ctx.emit({"type": "status",
                    "content": "⚡ Pre-Explore reused from cache (workspace unchanged)"})
                try:
                    _contracts_raw = parse_contract_summary(_explore_ctx)
                    _touched_labels = [
                        c.get("partition", "") for c in _contracts_raw
                        if c.get("touched_by_task") == "yes"
                    ]
                    _plan_ok, _plan_reason = _validate_explore_plan(
                        explore_ctx=_explore_ctx,
                        touched_partitions=_touched_labels,
                        min_steps=0,
                    )
                    if not _plan_ok:
                        logger.warning(
                            "[PRE-EXPLORE-CACHE-HIT] cached plan invalid: %s — fresh explore",
                            _plan_reason,
                        )
                        _explore_ctx = ""
                        _pre_explore_msgs = []
                        _contracts_raw = []
                    else:
                        from core.plan_tracker import build_tracker_from_contracts
                        _plan_tracker = build_tracker_from_contracts(_contracts_raw, workspace=_ws_str)
                        if _plan_tracker and _plan_tracker.total == 0:
                            _plan_tracker = None
                        for _ct in _contracts_raw:
                            if _ct.get("touched_by_task") == "yes":
                                for _f in _ct.get("files_read", []):
                                    _touched_paths.add(str(_f).replace("\\", "/").lower())
                except Exception as _cache_hit_err:
                    logger.warning("[PRE-EXPLORE-CACHE-HIT] post-parse failed: %s", _cache_hit_err)
                    _plan_tracker = None
    state.update({
        "_explore_ctx": _explore_ctx,
        "_pre_explore_msgs": _pre_explore_msgs,
        "_contracts_raw": _contracts_raw,
        "_plan_tracker": _plan_tracker,
        "_touched_paths": _touched_paths,
        "_ck": _ck,
        "_xexplore_mdl_early": _xexplore_mdl_early,
    })

async def _phase_pre_explore_prepare(ctx, state: dict):
    """P3: Setup (ctx/opts/budgets/msgs/tools/tree) — aus _phase_pre_explore extrahiert."""
    # PHASE-FIX (2026-09-02): unpack locals from state (lost during extraction).
    _xexplore_mdl_early = state.get("_xexplore_mdl_early") or ""
    _ws_str = state.get("_ws_str", "") or ""
    exec_mdl = state.get("exec_mdl", "") or ""
    _xtools_ws = state.get("_xtools_ws") or []
    _duo_ws = bool(state.get("_duo_ws"))
    _MAX_TREE_DEPTH = int(state.get("_MAX_TREE_DEPTH") or 4)
    _MAX_TREE_FILES = int(state.get("_MAX_TREE_FILES") or 200)
    logger.warning(
        "[PRE-EXPLORE-CACHE-MISS] No valid cache - starting fresh explore: mdl=%s ws=%r",
        _xexplore_mdl_early, (_ws_str or "")[:50]
    )
    yield await ctx.emit({"type": "status", "content": f"🔍 Pre-Explore: {exec_mdl} analyzing codebase…"})

    # ── Exploration-Agent Override ────────────────────────────────
    _xexplore_cfg = ctx.settings.get("exploration_agent") or {}
    _xexplore_mdl = exec_mdl
    if _xexplore_cfg.get("enabled"):
        _xcfg_model = (_xexplore_cfg.get("model") or "").strip()
        _xcfg_workers = _xexplore_cfg.get("workers") or []
        _xcfg_first = next(
            ((w.get("model") or "").strip() for w in _xcfg_workers if (w.get("model") or "").strip()),
            None
        )
        if ctx.duo_config.parallel_preexplore and _xcfg_first:
            _xexplore_mdl = _xcfg_first
        elif _xcfg_model:
            _xexplore_mdl = _xcfg_model
        elif _xcfg_first:
            _xexplore_mdl = _xcfg_first
    _ck = _explore_cache_key(_ws_str, _xexplore_mdl, ctx.user_input)
    logger.info("[PRE-EXPLORE] Worker model: %s (override: %s)", _xexplore_mdl, _xexplore_cfg.get("enabled", False))

    # ── Setup ────────────────────────────────────────────────────
    _xctx = int(
        ctx.settings.get("duo_pre_explore_ctx")
        or ctx.get_num_ctx(_xexplore_mdl, "duo_coder")
        or 4096
    )
    _xopts = {
        "temperature": 0.1,
        "num_predict": int(ctx.settings.get("duo_pre_explore_tokens", 700)),
        "num_ctx":     _xctx,
        # LLM-TIMEOUT-FIX: Per-Call Read-Timeout konfigurierbar.
        "_llm_read_timeout_s": float(ctx.settings.get("duo_pre_explore_llm_timeout_s", 600.0) or 600.0),
    }
    _xmax  = int(ctx.settings.get("duo_pre_explore_max_tools", 20))
    _x_timeout_s = int(ctx.settings.get("duo_pre_explore_timeout_seconds", 600) or 600)
    # duo_pre_explore_max_files_est (default 15) x per-file-s / workers.
    try:
        _nx_cfgs_est = (ctx.settings.get("exploration_agent") or {}).get("workers") or []
        _n_parts_est = max(1, len([w for w in _nx_cfgs_est if w.get("model")]))
        _n_workers_est = max(1, int(ctx.settings.get("duo_worker_slots", 2) or 2))
        _n_files_est = int(ctx.settings.get("duo_pre_explore_max_files_est", 15) or 15)
        _per_file_s_est = float(ctx.settings.get("duo_pre_explore_timeout_per_file_s", 20.0) or 20.0)
        _file_based_est = int(_n_files_est * _per_file_s_est / _n_workers_est + 120)
        _part_based_est = int(_n_parts_est * 150 / _n_workers_est + 60)
        _dynamic_timeout = max(_file_based_est, _part_based_est)
        _x_timeout_s = max(_x_timeout_s, _dynamic_timeout)
    except Exception:
        pass
    _x_timeout_s = max(60, min(3600, _x_timeout_s))
    _xctx_chars_ratio = float(ctx.settings.get("duo_pre_explore_ctx_char_ratio", 3.0) or 3.0)
    _xctx_chars_ratio = min(8.0, max(1.8, _xctx_chars_ratio))
    # WORKER-N_PARALLEL-FIX 355:
    # _use_parallel = bool(ctx.settings.duo_parallel_preexplore) — User-Toggle.
    # Konfig. Multi-Worker: exploration_agent.workers → je n_parallel=1.
    import math as _math  # MATH-IMPORT-FIX: must be available before first use here
    _parallel_setting_est = ctx.duo_config.parallel_preexplore
    _use_parallel = bool(_parallel_setting_est)
    _xworker_cfgs_for_est = (ctx.settings.get("exploration_agent") or {}).get("workers") or []
    _use_multi_worker_est = _use_parallel
    if _use_multi_worker_est:
        _xmsg_n_parallel_est = 1
    else:
        _xmsg_n_parallel_est = 1
    _xctx_slot_eff = max(512, _xctx // max(1, _xmsg_n_parallel_est))
    _XMSG_HARD_CAP    = max(_xctx * 8, _xctx_slot_eff * 8)
    #
    # Problem: Floor 28800 > _xctx_slot_eff*3.5*0.82 bei ctx≤8101:
    #   8101 * 3.5 * 0.82 = ~23248 chars echter KV-Budget
    #   → llama.cpp: "exceeds available context size" → Context-Limit-Recovery → 0 Reads
    #
    #   - echter-KV-Budget: _xctx_slot_eff * 3.5 * 0.80 (80% Sicherheitspuffer)
    #
    # Bei ctx=8101:  min(36454, 8101*3.5*0.80) = min(36454, 22683) = 22683 ✓
    # Bei ctx=16384: min(73728, 16384*3.5*0.80) = min(73728, 45875) = 45875 ✓
    # Bei ctx=32768: min(147456, 32768*3.5*0.80) = min(147456, 91750) = 91750 ✓
    _kv_budget_chars  = int(_xctx_slot_eff * 3.5 * 0.80)
    _ratio_limit      = int(_xctx_slot_eff * _xctx_chars_ratio)
    _XCTX_CHARS_LIMIT = max(12000, min(_ratio_limit, _kv_budget_chars))

    _xsys = (
        EXPLORE_CODEBASE_PROMPT + "\n"
        f"Workspace (project root): {_ws_str}\n"
        "ALWAYS use the full absolute workspace path for all tool calls.\n"
        "Available tools: read_file, get_signatures, list_dir, find_files, search_code — NOTHING ELSE.\n"
        "You CANNOT run bash commands, create directories, or write files.\n"
        "CRITICAL: You MUST use tools to explore the codebase FIRST.\n"
        "  1. Find existing relevant files\n"
        "  2. Understand classes, interfaces, and patterns in use\n"
        "  3. Identify dependencies and imports\n"
        "Do NOT write any files. Read and analyze only.\n"
        "End with a short implementation plan (max 5 points)."
    )
    _xmsgs = [
        {"role": "system", "content": _xsys},
        {"role": "user", "content": (
            f"Task: {ctx.user_input}\n\n"
            f"Explore the codebase. ALWAYS call at least ONE tool (read_file, get_signatures, find_files, etc.) to investigate the workspace BEFORE writing your summary.\n"
            f"Do NOT provide a plan before analyzing the file structures.\n"
            f"Once you gathered enough context, write a concise summary:\n"
            f"  - Relevant file paths\n"
            f"  - Key classes/methods (names, not full code)\n"
            f"  - Implementation plan: which files need changes and why\n"
            f"Keep summary under 200 words. The implementer will re-read files as needed."
        )},
    ]

    _xtools = _filter_tools_for_mode(
        _xtools_ws,
        mode="pre_explore",
        include_websearch=_duo_ws,
    )

    # ── FIX: Tree Scout — Workspace-Baum VOR Pre-Explore ────────────────────
    try:
        _tree_ctx = await get_workspace_tree(
            task      = ctx.user_input,
            ws_str    = _ws_str,
            client    = None,
            port      = 0,
            model     = exec_mdl,
            max_depth = int(ctx.settings.get("duo_tree_scout_max_depth", _MAX_TREE_DEPTH)),
            max_files = int(ctx.settings.get("duo_tree_scout_max_files", _MAX_TREE_FILES)),
            enabled   = bool(ctx.settings.get("duo_tree_scout_enabled", True)),
        )
        if _tree_ctx:
            _wmax_t, _wdepth_t = _window_params(ctx)
            _tree_window = await _get_analysis_window(_ws_str, _wmax_t, _wdepth_t)
            _tree_ctx = await _rebuild_tree_ctx_from_window(
                _tree_ctx, _ws_str, _tree_window, _wdepth_t,
            )
            _xmsgs.append({"role": "user", "content": _tree_ctx})
            _xmsgs[0] = {
                **_xmsgs[0],
                "content": _xmsgs[0]["content"].replace(
                    "Available tools: read_file, get_signatures, list_dir, find_files, search_code",
                    "Available tools: read_file, get_signatures, list_dir, find_files, search_code\n"
                    "The workspace tree is already provided — use read_file directly, "
                    "skip list_dir/find_files unless exploring unknown subdirectories.",
                ),
            }
    except Exception:
        _tree_ctx = ""

    # PATCH-3: Contract-Memory — Prior Knowledge aus letztem Run einblenden.
    # Bekannte Repos brauchen 60-80% weniger Pre-Explore-Runden.
    try:
        from patch_3_contract_memory import load_contract_memory, build_prior_knowledge_block
        _p3_prior_contracts = load_contract_memory(_ws_str, ctx.chat_id, _SESSIONS_DIR)
        _p3_prior_block = build_prior_knowledge_block(_p3_prior_contracts)
        if _p3_prior_block and _tree_ctx:
            _tree_ctx = _p3_prior_block + "\n\n" + _tree_ctx
        elif _p3_prior_block:
            _tree_ctx = _p3_prior_block
    except Exception:
        pass

    _exec_has_thinking = ctx.model_profile(exec_mdl
    ).get("thinking", False)
    if _exec_has_thinking:
        _xmsgs = _inject_no_think_directive(_xmsgs)

    _xexplore_t = time.time()
    ctx.phase_timer.start("pre_explore")
    state.update({
        "_xexplore_mdl": _xexplore_mdl,
        "_ck": _ck,
        "_xctx": _xctx,
        "_xopts": _xopts,
        "_xmax": _xmax,
        "_x_timeout_s": _x_timeout_s,
        "_XMSG_HARD_CAP": _XMSG_HARD_CAP,
        "_XCTX_CHARS_LIMIT": _XCTX_CHARS_LIMIT,
        "_xmsgs": _xmsgs,
        "_xtools": _xtools,
        "_tree_ctx": _tree_ctx,
        "_xexplore_t": _xexplore_t,
        "_use_parallel": _use_parallel,
    })

async def _phase_pre_explore_finalize(ctx, state: dict):
    """P7: Static-Map-Merge, Success-Emits, Unified-Evict, Persistence,
    Contract-Merge — aus _phase_pre_explore extrahiert (mechanisch)."""
    _static_map_task = state.get("_static_map_task")
    _explore_ctx = state.get("_explore_ctx")
    _tree_ctx = state.get("_tree_ctx")
    _ws_str = state.get("_ws_str")
    _worker_slots = state.get("_worker_slots")
    _xexplore_mdl = state.get("_xexplore_mdl")
    _use_parallel = state.get("_use_parallel")
    _partitions = state.get("_partitions")
    _xparallel_results = state.get("_xparallel_results")
    _xexplore_t = state.get("_xexplore_t")
    _duo_pinned = state.get("_duo_pinned")
    _ck = state.get("_ck")
    _pre_explore_msgs = state.get("_pre_explore_msgs")
    _chat_ctx_loaded = state.get("_chat_ctx_loaded")
    _xmsgs = state.get("_xmsgs")
    _touched_paths = state.get("_touched_paths") or set()

    if _static_map_task is not None:
        try:
            _static_map_result = await _static_map_task
            if isinstance(_static_map_result, str) and _static_map_result:
                _explore_ctx = _static_map_result + "\n\n" + (_explore_ctx or "")
                logger.info("[STATIC-REPO-MAP] merged %d chars into explore_ctx", len(_static_map_result))
                try:
                    from tools.runner import _current_project_state
                    ps = _current_project_state.get()
                    if ps is not None:
                        import hashlib as _hl_rh
                        repomap_data = json.dumps(_explore_ctx, sort_keys=True, default=str)
                        ps.repomap_hash = _hl_rh.sha256(repomap_data.encode()).hexdigest()[:16]
                        from context.project_state import ProjectStateManager
                        ProjectStateManager().save(ps)
                except Exception as _rh_err:
                    logger.warning("[PROJECT] Repomap hash-save failed: %s", _rh_err)
            elif not _static_map_result:
                logger.info("[STATIC-REPO-MAP] empty result")
        except Exception as _sm_err:
            logger.warning("[STATIC-REPO-MAP] failed: %s", _sm_err)
    elif not _explore_ctx and _tree_ctx and _ws_str:
        try:
            from hive_functions.static_repomap import build_static_repomap
            from hive_functions.ctx_utils import derive_static_map_budget
            _wmax_fb, _wdepth_fb = _window_params(ctx)
            _sm_window_fb = await _get_analysis_window(_ws_str, _wmax_fb, _wdepth_fb)
            _sm_parts_fb = await partition_tree_async(
                _tree_ctx,
                max_files_per_partition=int(ctx.settings.get("duo_partition_max_files", 30)),
                workspace_root=_ws_str,
                preselect_paths=_sm_window_fb,
            )
            if _sm_parts_fb:
                _sm_budget_fb = derive_static_map_budget(
                    state.get("_exec_ctx"),
                    ctx.settings.get("duo_planner_ctx_target", 0),
                    ctx.settings.get("duo_static_map_chars", 0),
                )
                _sm_res_fb = await build_static_repomap(_ws_str, _sm_parts_fb, char_budget=_sm_budget_fb)
                if isinstance(_sm_res_fb, str) and _sm_res_fb:
                    _explore_ctx = _sm_res_fb
                    logger.info(
                        "[STATIC-REPO-MAP-FALLBACK] %d chars, %d partitions — "
                        "LLM Pre-Explore not started (VRAM/port)",
                        len(_sm_res_fb), len(_sm_parts_fb),
                    )
                    yield await ctx.emit({"type": "status",
                        "content": "📋 Static repo-map fallback (LLM pre-explore not started)"})
                    try:
                        from tools.runner import _current_project_state
                        ps = _current_project_state.get()
                        if ps is not None:
                            import hashlib as _hl_rh
                            repomap_data = json.dumps(_explore_ctx, sort_keys=True, default=str)
                            ps.repomap_hash = _hl_rh.sha256(repomap_data.encode()).hexdigest()[:16]
                            from context.project_state import ProjectStateManager
                            ProjectStateManager().save(ps)
                    except Exception as _rh_fb_err:
                        logger.warning("[PROJECT] Repomap hash-save failed: %s", _rh_fb_err)
        except Exception as _sm_fb_err:
            logger.warning("[STATIC-REPO-MAP-FALLBACK] failed: %s", _sm_fb_err)

    if _explore_ctx:
        yield await ctx.emit({"type": "status", "content": "✅ Exploration complete — plan created"})
        _x_models = []
        for _ws in (_worker_slots or []):
            _wm = str(_ws.get("model") or "").strip()
            if _wm and _wm not in _x_models:
                _x_models.append(_wm)
        if not _x_models and _xexplore_mdl:
            _x_models.append(_xexplore_mdl)
        if _x_models:
            yield await ctx.emit({"type": "run_meta", "models": {"pre_explore": _x_models}})
        # n_files_total aus _partitions, n_files_read aus partition_done-Events (in all_results).
        if _use_parallel and _partitions:
            try:
                _cov_total = sum(len(p.get("paths", [])) for p in _partitions)
                _cov_read  = sum(
                    r.get("contract", {}).get("files_read") and
                    len(r["contract"].get("files_read", [])) or 0
                    for r in _xparallel_results
                )
                if _cov_total > 0:
                    _cov_pct = int(100 * _cov_read / _cov_total)
                    yield await ctx.emit({
                        "type": "status",
                        "content": f"📊 Coverage: {_cov_read}/{_cov_total} Dateien gelesen ({_cov_pct}%)",
                    })
            except Exception:
                pass
    yield await ctx.emit({"type": "agent_done", "elapsed": round(time.time() - _xexplore_t, 1)})
    _explore_ctx_str = _explore_ctx or ""
    _explore_is_timeout = _explore_ctx_str.startswith("[Pre-Explore timeout")
    _explore_is_error   = _explore_ctx_str.startswith("[Exploration fehlgeschlagen")
    ctx.phase_timer.end(
        "pre_explore",
        status="timeout" if _explore_is_timeout else "ok",
    )
    if not _explore_ctx_str or _explore_is_error:
        yield await ctx.emit({
            "type": "status",
            "content": "⚠️ Exploration complete — no context available",
        })
    yield await ctx.emit({
        "type": "pre_explore_done",
        "elapsed": round(time.time() - _xexplore_t, 1),
        "status": "timeout" if _explore_is_timeout else ("error" if _explore_is_error else "ok"),
        "has_ctx": bool(_explore_ctx_str and not _explore_is_error and not _explore_is_timeout),
    })

    # Batch 1.1: Triple→Single Eviction. Pre-Explore Worker + Coder/Critic
    try:
        from backend.llama_server_manager import manager as _lsm_post_explore
        _pe_evicted = 0
        for _pinned_mdl in list(_duo_pinned):
            try:
                await _lsm_post_explore.evict(_pinned_mdl)
                _duo_pinned.discard(_pinned_mdl)
                _pe_evicted += 1
            except Exception:
                pass
        _pe_loaded = await _lsm_post_explore.list_loaded()
        for _pe_slot in (_pe_loaded or []):
            _pe_name = str(_pe_slot.get("name") or _pe_slot.get("model") or "")
            if _pe_name:
                try:
                    await _lsm_post_explore.evict(_pe_name)
                    _pe_evicted += 1
                except Exception:
                    pass
        # Phase 3: AMD/Vulkan VRAM-Settle + Gate
        await asyncio.sleep(3.0)
        _pe_deadline = time.time() + 10.0
        _pe_still_loaded = await _lsm_post_explore.list_loaded()
        while _pe_still_loaded and time.time() < _pe_deadline:
            await asyncio.sleep(0.5)
            _pe_still_loaded = await _lsm_post_explore.list_loaded()
        if _pe_still_loaded:
            _pe_names = [m.get("name", "?") for m in _pe_still_loaded]
            logger.warning("[EVICT WARNING] Models still loaded after unified evict: %s", _pe_names)
        else:
            logger.info("[EVICT CHECK] Loaded after unified evict: []")
        logger.info(f"[UNIFIED-EVICT] {_pe_evicted} model(s) unloaded - VRAM free for planner/coder")
        yield await ctx.emit({"type": "status",
            "content": f"🗑️ {_pe_evicted} model(s) unloaded — VRAM free for coder/planner"})
        try:
            from backend.llama_compat import force_kill_all
            await force_kill_all()
            _kill_deadline = time.time() + 8.0
            while time.time() < _kill_deadline:
                _still_loaded = await _lsm_post_explore.list_loaded()
                if not _still_loaded:
                    break
                await asyncio.sleep(0.5)
            else:
                logger.warning("[EVICT] force_kill_all() timeout — VRAM may not be fully released")
            yield await ctx.emit({"type": "status",
                "content": "🗑️ VRAM bereinigt — frischer Start fuer Planner"})
        except Exception:
            pass

        await asyncio.sleep(2.5)  # Vulkan-Reclaim
    except Exception as _pe_evict_err:
        logger.debug(f"[UNIFIED-EVICT] Failed (not critical): {_pe_evict_err}")

    _pre_explore_msgs = (
        [m for m in _xmsgs if isinstance(m, dict) and "role" in m
         and (m.get("content") is not None or m.get("tool_calls"))]
        if _explore_ctx else []
    )
    logger.info("[PRE-EXPLORE-RESULT] _explore_ctx_len=%d, _pre_explore_msgs=%d msgs",
                len(_explore_ctx) if _explore_ctx else 0,
                len(_pre_explore_msgs))

    # Save to RAM cache
    if _explore_ctx and not _explore_ctx.startswith("["):
        yield await ctx.emit({"type": "status", "content": "💾 Processing and saving project metadata…"})
        _file_snapshot = _explore_extract_files(_explore_ctx, _pre_explore_msgs)
        async with _get_pre_explore_lock():
            _now_evict = time.time()
            _stale = [k for k, v in _pre_explore_cache.items()
                       if _now_evict - v.get("ts", 0) > _PRE_EXPLORE_CACHE_TTL]
            for _sk in _stale:
                del _pre_explore_cache[_sk]
        await _pre_explore_cache_set(_ck, {
            "ctx":       _explore_ctx,
            "msgs":      _pre_explore_msgs,
            "files":     _file_snapshot,
            "ts":        time.time(),
            "workspace": str(Path(_ws_str).resolve()),
        })
        _p3_contracts = None
        try:
            from patch_3_contract_memory import save_contract_memory
            _p3_contracts = parse_contract_summary(_explore_ctx)
            if _p3_contracts and ctx.chat_id:
                save_contract_memory(
                    _ws_str, ctx.chat_id, _SESSIONS_DIR,
                    _p3_contracts, (ctx.user_input or "")[:200],
                )
        except Exception:
            pass
        # Extract touched-file paths from contracts for context density filter
        if _p3_contracts:
            for _ct in _p3_contracts:
                if _ct.get("touched_by_task") == "yes":
                    for _f in _ct.get("files_read", []):
                        _touched_paths.add(str(_f).replace("\\", "/").lower())
        # Save .context.json next to the chat file (disk persistence)
        if ctx.chat_id:
            _files_read_list = []
            for _msg in _pre_explore_msgs:
                _mc = _msg.get("content", "")
                if isinstance(_mc, str):
                    for _fp in _RE_FILE_EXT.findall(_mc):
                        if _fp not in _files_read_list:
                            _files_read_list.append(_fp)
            _save_chat_context(ctx.chat_id, {
                "workspace":    _ws_str,
                "explore_ctx":  _explore_ctx,
                "files_read":   _files_read_list,
                "files":        _file_snapshot,
                "ts":           time.time(),
                "touched_paths": list(_touched_paths) if _touched_paths else [],
                "tree_ctx":     _tree_ctx if _tree_ctx else "",
                "run_count":    (_chat_ctx_loaded.get("run_count", 0) + 1) if _chat_ctx_loaded else 1,
                "task_sig":     hashlib.md5(ctx.user_input[:60].encode()).hexdigest()[:8],
            })
            yield await ctx.emit({"type": "status",
                "content": "💾 Chat context saved — next input starts immediately"})
    ctx.clear_step_skip(ctx.run_id)

    _plan_tracker = None
    _contracts_raw = []
    if _explore_ctx and not _explore_ctx.startswith("["):
        try:
            _contracts_raw = parse_contract_summary(_explore_ctx)
            # Validate: must reference touched partitions and contain actions
            _touched_partition_labels = [
                c.get("partition", "")
                for c in _contracts_raw
                if c.get("touched_by_task") == "yes"
            ]
            _plan_valid, _plan_reason = _validate_explore_plan(
                explore_ctx=_explore_ctx,
                touched_partitions=_touched_partition_labels,
                min_steps=0,
            )
            if not _plan_valid:
                logger.warning(
                    "[PRE-EXPLORE VALIDATION] Plan invalid: %s. "
                    "Coder will start with fallback explore instruction.", _plan_reason
                )
                _explore_ctx = ""
                _pre_explore_msgs = []
            from core.plan_tracker import build_tracker_from_contracts, PlanTracker
            _plan_tracker = build_tracker_from_contracts(_contracts_raw, workspace=_ws_str)
            if _plan_tracker and _plan_tracker.total == 0:
                _plan_tracker = None
        except Exception:
            _plan_tracker = None
    # CONTRACT-MERGE: merge freshly parsed contracts with persisted
    # contracts from .context.json. New partitions added, existing
    # updated, partitions with no surviving files pruned.
    if _contracts_raw and ctx.chat_id and _chat_ctx_loaded:
        _new_contracts: dict = {}
        for c in _contracts_raw:
            pk = str(c.get("partition", "")).strip()
            if pk:
                _new_contracts[pk] = {
                    "files_read": c.get("files_read", []),
                    "entry_points": c.get("entry_points", []),
                    "exports": c.get("exports", []),
                    "role": c.get("role", ""),
                    "hint": c.get("hint", ""),
                }
        _persisted_cts = (_chat_ctx_loaded or {}).get("contracts", {})
        for pk, ct in _new_contracts.items():
            _persisted_cts[pk] = ct
        # Prune partitions whose files no longer exist on disk
        _ws_path_obj = Path(_ws_str)
        _merged: dict = {}
        for pk, ct in _persisted_cts.items():
            _files = ct.get("files_read", [])
            if any((_ws_path_obj / str(f)).exists() for f in _files):
                _merged[pk] = ct
        if _merged:
            try:
                _fresh_ctx = _load_chat_context(ctx.chat_id) or {}
                _save_chat_context(ctx.chat_id, {
                    **_fresh_ctx,
                    "contracts": _merged,
                })
            except Exception:
                pass
    if _plan_tracker is None and _explore_ctx and not _explore_ctx.startswith("["):
        try:
            from core.plan_tracker import PlanTracker
            _plan_tracker = PlanTracker([{"step": 1, "file": "?", "action": (ctx.user_input or "")[:80]}])
        except Exception:
            _plan_tracker = None
    state.update({
        "_explore_ctx": _explore_ctx,
        "_pre_explore_msgs": _pre_explore_msgs,
        "_touched_paths": _touched_paths,
        "_plan_tracker": _plan_tracker,
        "_contracts_raw": _contracts_raw,
        "_duo_pinned": _duo_pinned,
    })

async def _phase_pre_explore(ctx, state: dict):
    _ws_str = state["_ws_str"]
    _duo_seen_web_queries = state["_duo_seen_web_queries"]
    exec_mdl = state["exec_mdl"]
    _exec_tc = state["_exec_tc"]
    _coder_tc = state["_coder_tc"]
    _avail = state["_avail"]
    _duo_ws = state["_duo_ws"]
    _xtools_ws = state["_xtools_ws"]
    _MAX_TREE_DEPTH = state["_MAX_TREE_DEPTH"]
    _MAX_TREE_FILES = state["_MAX_TREE_FILES"]
    _recent_focus_paths = state["_recent_focus_paths"]
    _duo_pinned = state["_duo_pinned"]

    async for _ev in _phase_pre_explore_bootstrap(ctx, state):
        yield _ev
    _ws_str = state["_ws_str"]
    _explore_ctx = state["_explore_ctx"]
    _tree_ctx = state["_tree_ctx"]
    _resume_data = state["_resume_data"]
    _pre_explore_msgs = state["_pre_explore_msgs"]
    _touched_paths = state["_touched_paths"]
    _chat_ctx_loaded = state["_chat_ctx_loaded"]
    _x_thinking_override = state["_x_thinking_override"]
    _parallel_explore_ran = state["_parallel_explore_ran"]
    _duo_pre_explore_user_explicit = state["_duo_pre_explore_user_explicit"]
    _contracts_raw = state["_contracts_raw"]
    _plan_tracker = state["_plan_tracker"]
    _worker_slots = state["_worker_slots"]
    _workers_were_loaded = state["_workers_were_loaded"]
    _use_parallel = state["_use_parallel"]
    _xexplore_mdl = state["_xexplore_mdl"]
    _partitions = state["_partitions"]
    _effective_max_files = state["_effective_max_files"]
    _static_map_task = state["_static_map_task"]
    _xtask = state["_xtask"]

    async for _ev in _phase_pre_explore_cache(ctx, state):
        yield _ev
    _explore_ctx = state["_explore_ctx"]
    _pre_explore_msgs = state["_pre_explore_msgs"]
    _contracts_raw = state["_contracts_raw"]
    _plan_tracker = state["_plan_tracker"]
    _touched_paths = state["_touched_paths"]
    _ck = state["_ck"]
    _xexplore_mdl_early = state["_xexplore_mdl_early"]

    if ctx.duo_config.pre_explore and not _resume_data and not _explore_ctx:  # EMPTY-WORKSPACE-SKIP
        async for _ev in _phase_pre_explore_prepare(ctx, state):
            yield _ev
        _xexplore_mdl = state["_xexplore_mdl"]
        _ck = state["_ck"]
        _xctx = state["_xctx"]
        _xopts = state["_xopts"]
        _xmax = state["_xmax"]
        _x_timeout_s = state["_x_timeout_s"]
        _XMSG_HARD_CAP = state["_XMSG_HARD_CAP"]
        _XCTX_CHARS_LIMIT = state["_XCTX_CHARS_LIMIT"]
        _xmsgs = state["_xmsgs"]
        _xtools = state["_xtools"]
        _tree_ctx = state["_tree_ctx"]
        _xexplore_t = state["_xexplore_t"]
        _use_parallel = state["_use_parallel"]
        # PHASE-FIX (2026-09-02): local `_xexplore_cfg` was lost during the
        # phase extraction — recompute it (same source as _phase_pre_explore_prepare).
        _xexplore_cfg = ctx.settings.get("exploration_agent") or {}

        try:
            from backend.llama_server_manager import manager as _lsm2

            # ── Parallel vs. sequenzieller Pre-Explore — VOR Port-Lookup ─
            #
            # USER-TOGGLE: duo_parallel_preexplore = true/false (aus UI-Toggle).
            _worker_cfgs = _xexplore_cfg.get("workers") or []
            _parallel_setting = bool(ctx.duo_config.parallel_preexplore)
            _use_parallel = bool(_parallel_setting and _worker_cfgs)
            if _parallel_setting and not _worker_cfgs:
                yield await ctx.emit({"type": "status",
                    "content": "⚠️ Parallel pre-explore active, but no workers configured — falling back to sequential"})

            _skip_xexplore_load = (
                _use_parallel
                and bool(_worker_cfgs)
                and _xexplore_mdl == exec_mdl
            )
            if _skip_xexplore_load:
                _xport = 0
            else:
                _xport = next(
                    (_sl.port for _sl in _lsm2._slots if _sl.model == _xexplore_mdl and _sl.is_running),
                    None
                )
                if _xport is None:
                    yield await ctx.emit({"type": "status", "content": f"⏳ Pre-Explore: loading {_xexplore_mdl} into VRAM…"})
                    _xexplore_load_timeout = 150.0
                    try:
                        _xport = await asyncio.wait_for(
                            _lsm2.ensure_loaded(_xexplore_mdl, num_ctx=_xopts.get("num_ctx", 4096)),
                            timeout=_xexplore_load_timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            "[Pre-Explore] ensure_loaded timeout (%.0fs) for %s - pre-explore aborted",
                            _xexplore_load_timeout, _xexplore_mdl,
                        )
                        yield await ctx.emit({"type": "status",
                            "content": f"⛔ Pre-Explore: model {_xexplore_mdl.split(':')[0]} not responding "
                                       f"after {int(_xexplore_load_timeout)}s — pre-explore skipped."})
                        try:
                            await _lsm2.evict(_xexplore_mdl)
                        except Exception:
                            pass
                        _explore_ctx = ""
                        _xport = None
                else:
                    yield await ctx.emit({"type": "status", "content": f"⚡ Pre-Explore: using pinned {_xexplore_mdl} (port {_xport})"})

            _xexplore_t = time.time()

            if _xexplore_mdl != exec_mdl:
                yield await ctx.emit({"type": "status",
                    "content": f"🔍 Exploration agent: {_xexplore_mdl} (override for pre-explore)"})

            # exploration_agent.workers = [{"model": "qwen3.5:2b", "ctx": 4096}, ...]
            # Fallback: single model (_xexplore_mdl / _xport) wie bisher.
            # {"model":"qwen3.5:2b"}, {"model":"qwen3.5:2b"} → :2b + :2b#2 (selbe GGUF, 2 Ports)
            _worker_slots: list[dict] = []
            _workers_were_loaded: bool = False
            if _worker_cfgs and _use_parallel:
                from backend.llama_server_manager import manager as _lsm_workers
                from backend.llama_vram_table import vram_of_with_ctx as _vram_ctx
                _worker_fail_reasons: dict[str, str] = {}
                _planned_workers: list[dict] = []
                _worker_bases = {(w.get("model") or "").strip().rsplit("#", 1)[0] for w in _worker_cfgs if w.get("model")}
                _ve_loaded = await _lsm_workers.list_loaded()
                _ve_loaded_names = [
                    str(s.get("name") or s.get("model") or "")
                    for s in _ve_loaded
                ]
                logger.info(
                    "[PRE-EXPLORE-VOREVICT] loaded=%s worker_bases=%s",
                    _ve_loaded_names, sorted(_worker_bases),
                )
                _running_non_worker = []
                for _sl in _lsm_workers._slots:
                    if not (_sl.is_running and _sl.model):
                        continue
                    _sl_base = str(_sl.model).rsplit("#", 1)[0]
                    if _sl_base in _worker_bases:
                        continue
                    _running_non_worker.append(str(_sl.model))

                _evict_models = sorted(set(_running_non_worker))
                for _m in _evict_models:
                    logger.info(
                        "[PRE-EXPLORE-VOREVICT] evicting slot '%s' (not in worker_bases)",
                        _m,
                    )
                    yield await ctx.emit({
                        "type": "status",
                        "content": f"⏳ Pre-Explore: evicting {_m} for worker VRAM…",
                    })
                    try:
                        await _lsm_workers.evict(_m)
                    except Exception:
                        pass
                logger.info(
                    "[PRE-EXPLORE-VOREVICT] done — %d slot(s) evicted",
                    len(_evict_models),
                )

                _budget_gb = ctx.vram_budget
                _pinned_other_gb = 0.0
                for _sl in _lsm_workers._slots:
                    if not (_sl.is_running and _sl.pinned and _sl.model):
                        continue
                    _sl_base = str(_sl.model).rsplit("#", 1)[0]
                    if _sl_base in _worker_bases:
                        continue
                    _sl_ctx = int(getattr(_sl, "_num_ctx", 0) or 4096)
                    _pinned_other_gb += float(_vram_ctx(str(_sl.model), _sl_ctx))

                _headroom_gb = max(0.0, _budget_gb - _pinned_other_gb)
                _worker_cfgs_valid = [w for w in _worker_cfgs if (w.get("model") or "").strip()]
                _requested_workers = len(_worker_cfgs_valid)
                _selected_cfgs: list[dict] = []
                _cap_skipped_cfgs: list[dict] = []
                _selected_gb = 0.0
                for _wc in _worker_cfgs_valid:
                    _mdl = (_wc.get("model") or "").strip()
                    if not _mdl:
                        continue
                    _ctx = int(_wc.get("ctx") or _xctx)
                    _need_gb = float(_vram_ctx(_mdl, _ctx))
                    if _selected_cfgs and (_selected_gb + _need_gb) > _headroom_gb:
                        _cap_skipped_cfgs.append({"model": _mdl, "ctx": _ctx, "need_gb": _need_gb})
                        continue
                    _selected_cfgs.append(_wc)
                    _selected_gb += _need_gb

                if _selected_cfgs and len(_selected_cfgs) < _requested_workers:
                    _cap_msg = (
                        f"ℹ️ Worker-Cap aktiv: {len(_selected_cfgs)} statt {_requested_workers} "
                        f"(Headroom ~{_headroom_gb:.1f}GB, geplant ~{_selected_gb:.1f}GB)"
                    )
                    yield await ctx.emit({
                        "type": "status",
                        "content": _cap_msg,
                    })
                    yield await ctx.emit({
                        "type": "worker_pool_state",
                        "phase": "cap",
                        "target_workers": _requested_workers,
                        "active_workers": len(_selected_cfgs),
                        "missing": [
                            {
                                "model": str(_cw.get("model") or "?"),
                                "reason": (
                                    f"Worker-Cap/VRAM: benötigt ~{float(_cw.get('need_gb') or 0.0):.1f}GB, "
                                    f"Headroom ~{_headroom_gb:.1f}GB"
                                ),
                            }
                            for _cw in _cap_skipped_cfgs
                        ],
                    })
                _worker_cfgs = _selected_cfgs if _selected_cfgs else _worker_cfgs

                yield await ctx.emit({"type": "status",
                    "content": f"⏳ Pre-Explore: loading {len(_worker_cfgs)} worker slot(s)…"})
                _wmdl_seen: dict[str, int] = {}
                _n_worker_cfgs = len([w for w in _worker_cfgs if (w.get("model") or "").strip()])
                # PARALLEL-DEFAULT (WORKER-N_PARALLEL-FIX 355):
                # - 1 Worker (Single-Worker-Fallback): ceil(parts/1) = parts → mind. 2,
                #
                import math as _math
                # eigener voller KV-Cache).
                _default_n_parallel = 1
                _default_multi_worker_parallel = 1  # WORKER-N_PARALLEL-FIX 355
                for _wi, _wc in enumerate(_worker_cfgs):
                    _wmdl_base = (_wc.get("model") or "").strip()
                    _wctx = int(_wc.get("ctx") or _xctx)
                    if not _wc.get("ctx"):
                        _wctx = max(_wctx, 16384)

                    _has_parallel_override = _wc.get("parallel") is not None
                    if _has_parallel_override:
                        _wn_parallel = int(_wc.get("parallel") or 1)
                    else:
                        _wn_parallel = (
                            _default_multi_worker_parallel
                            if _n_worker_cfgs > 1
                            else _default_n_parallel
                        )
                    if not _wmdl_base:
                        continue
                    # Auto-Alias: zweites "qwen3.5:2b" → "qwen3.5:2b#2", drittes → "#3" etc.
                    _wmdl_seen[_wmdl_base] = _wmdl_seen.get(_wmdl_base, 0) + 1
                    _wmdl = _wmdl_base if _wmdl_seen[_wmdl_base] == 1 else f"{_wmdl_base}#{_wmdl_seen[_wmdl_base]}"
                    _planned_workers.append({
                        "worker_index": _wi,
                        "model": _wmdl,
                        "num_ctx": _wctx,
                        "n_parallel": _wn_parallel,
                        "parallel_override": bool(_has_parallel_override),
                    })
                    try:
                        yield await ctx.emit({"type": "status",
                            "content": f"⏳ Pre-Explore: loading worker {_wi}: {_wmdl} (ctx={_wctx})…"})
                        _wport = await asyncio.wait_for(
                            _lsm_workers.ensure_loaded(_wmdl, num_ctx=_wctx, n_parallel=_wn_parallel, pin=True),
                            timeout=150.0,
                        )
                        # Post-check: verify the slot actually came up
                        _ws_loaded_post = await _lsm_workers.list_loaded()
                        _ws_loaded_names_post = {
                            str(s.get("name") or s.get("model") or "")
                            for s in _ws_loaded_post
                        }
                        _wmdl_base_check = _wmdl.rsplit("#", 1)[0]
                        if not any(_wmdl_base_check in n for n in _ws_loaded_names_post):
                            logger.warning(
                                "[PRE-EXPLORE] Worker '%s' not confirmed in loaded "
                                "slots after ensure_loaded — slot may have failed to start. "
                                "Loaded: %s",
                                _wmdl, sorted(_ws_loaded_names_post),
                            )
                        _worker_slots.append({"model": _wmdl, "port": _wport, "n_parallel": _wn_parallel, "num_ctx": _wctx})
                        _alias_hint = f" [alias → selbe GGUF wie {_wmdl_base}]" if "#" in _wmdl else ""
                        yield await ctx.emit({"type": "status",
                            "content": f"  ✓ Worker {_wi}: {_wmdl} (port {_wport}, ctx={_wctx}, parallel={_wn_parallel}){_alias_hint}"})
                    except Exception as _we:
                        logger.warning("Worker slot %s (%s) could not be loaded: %s",
                                       _wi, _wmdl, _we)
                        _wreason = str(_we)[:220]
                        _worker_fail_reasons[_wmdl] = f"load: {_wreason}"
                        # ── GRACEFUL WORKER FALLBACK ──────────────────────────────
                        _WORKER_FALLBACK_ORDER = [
                            "granite-4.1:3b", "granite4:1b",
                            "qwen3.5:4b", "qwen3.5:0.8b",
                        ]
                        _fallback_loaded = False
                        for _fb_mdl in _WORKER_FALLBACK_ORDER:
                            if _fb_mdl == _wmdl_base:
                                continue
                            from backend.llama_models import resolve_model_path as _rmp
                            _fb_path = _rmp(_fb_mdl)
                            if not _fb_path:
                                continue
                            if ".ollama" in str(_fb_path).replace("\\", "/"):
                                continue
                            yield await ctx.emit({"type": "status",
                                "content": f"  🔄 Worker fallback: {_wmdl} failed → trying {_fb_mdl}…"})
                            try:
                                _fb_port = await asyncio.wait_for(
                                    _lsm_workers.ensure_loaded(_fb_mdl, num_ctx=_wctx, n_parallel=_wn_parallel, pin=True),
                                    timeout=150.0,
                                )
                                _worker_slots.append({"model": _fb_mdl, "port": _fb_port, "n_parallel": _wn_parallel, "num_ctx": _wctx})
                                yield await ctx.emit({"type": "status",
                                    "content": f"  ✓ Worker fallback loaded: {_fb_mdl} (port {_fb_port}, ctx={_wctx})"})
                                _fallback_loaded = True
                                break
                            except Exception as _fb_exc:
                                logger.warning("Worker fallback %s also failed: %s", _fb_mdl, _fb_exc)
                                continue
                        if not _fallback_loaded:
                            yield await ctx.emit({
                                "type": "worker_slot_failed",
                                "phase": "load",
                                "worker_index": _wi,
                                "model": _wmdl,
                                "ctx": _wctx,
                                "parallel": _wn_parallel,
                                "reason": _wreason,
                            })
                            yield await ctx.emit({"type": "status",
                                "content": f"⚠️ Worker {_wi} could not start: {_wmdl} — {_wreason}"})
                if _worker_slots:
                    # in Pass-2 check → ensure_loaded cascade. Now 1.5s.
                    await asyncio.sleep(1.5)
                    _refreshed: list[dict] = []
                    for _ws in list(_worker_slots):
                        _ws_base = _ws["model"].rsplit("#", 1)[0]
                        _ws_cfg  = next(
                            (c for c in _worker_cfgs if (c.get("model") or "").strip() == _ws_base),
                            {}
                        )
                        _ws_ctx        = int(_ws.get("num_ctx") or _ws_cfg.get("ctx") or _xctx)
                        _ws_n_parallel = _ws.get("n_parallel", 1)
                        _ws_port       = int(_ws.get("port", 0) or 0)
                        try:
                            # Cascade off. Now: 3 attempts × 0.4s = 1.2s patience.
                            if _ws_port > 0 and await _p2_alive(_ws_port, _lsm_workers):
                                _refreshed.append({
                                    "model": _ws["model"],
                                    "port": _ws_port,
                                    "n_parallel": _ws_n_parallel,
                                    "num_ctx": _ws_ctx,
                                })
                                continue

                            _rport = await _lsm_workers.ensure_loaded(
                                _ws["model"],
                                num_ctx=_ws_ctx,
                                n_parallel=_ws_n_parallel,
                                pin=True,
                            )
                            _refreshed.append({
                                "model": _ws["model"],
                                "port": _rport,
                                "n_parallel": _ws_n_parallel,
                                "num_ctx": _ws_ctx,
                            })
                            yield await ctx.emit({
                                "type": "status",
                                "content": f"  🔄 Worker {_ws['model']} reloaded (port {_ws_port}→{_rport})",
                            })
                        except Exception as _re_verify_exc:
                            logger.warning("Worker re-verify failed (%s): %s - slot will be skipped",
                                           _ws["model"], _re_verify_exc)
                            _rv_reason = str(_re_verify_exc)[:220]
                            _worker_fail_reasons[str(_ws["model"])] = f"reverify: {_rv_reason}"
                            yield await ctx.emit({
                                "type": "worker_slot_failed",
                                "phase": "reverify",
                                "model": _ws["model"],
                                "port": int(_ws.get("port", 0) or 0),
                                "reason": _rv_reason,
                            })
                            yield await ctx.emit({"type": "status",
                                "content": f"⚠️ Worker re-verify failed: {_ws['model']} — {_rv_reason}"})
                    _final: list[dict] = []
                    _seen_ports: set[int] = set()
                    for _rs in _refreshed:
                        _rport = int(_rs.get("port", 0) or 0)
                        if _rport in _seen_ports:
                            continue
                        if await _port_alive_with_retry(_rport, _lsm_workers, tries=5):
                            _final.append(_rs)
                            _seen_ports.add(_rport)
                        else:
                            logger.warning(
                                "STALE-PORT-GUARD Pass3: Port %d (%s) tot — entfernt",
                                _rs["port"], _rs["model"]
                            )
                            _worker_fail_reasons[str(_rs["model"])] = "pass3_stale: Port tot nach Re-Verify"
                            yield await ctx.emit({
                                "type": "worker_slot_failed",
                                "phase": "pass3_stale",
                                "model": _rs["model"],
                                "port": int(_rs.get("port", 0) or 0),
                                "reason": "Port dead after re-verify",
                            })
                            yield await ctx.emit({"type": "status",
                                "content": f"⚠️ STALE-PORT-GUARD: port {_rs['port']} ({_rs['model']}) dead — removed"})
                    _worker_slots = _final

                if _planned_workers:
                    _active_models = {str(_ws.get("model")) for _ws in _worker_slots}
                    _missing_plans = [
                        _pw for _pw in _planned_workers
                        if str(_pw.get("model")) not in _active_models
                    ]
                    if _missing_plans:
                        yield await ctx.emit({
                            "type": "status",
                            "content": (
                                f"↻ Worker-Recovery: {len(_missing_plans)} fehlende Slot(s) — "
                                "zweiter Ladeversuch (deferred)"
                            ),
                        })
                        for _mp in _missing_plans:
                            _mp_model = str(_mp.get("model") or "?")
                            _mp_ctx = int(_mp.get("num_ctx") or _xctx)
                            _mp_parallel = int(_mp.get("n_parallel") or 1)
                            if not _mp.get("parallel_override"):
                                _mp_parallel = 1
                            _mp_parallel = max(1, _mp_parallel)
                            try:
                                await asyncio.sleep(0.35)
                                _mp_port = await _lsm_workers.ensure_loaded(
                                    _mp_model,
                                    num_ctx=_mp_ctx,
                                    n_parallel=_mp_parallel,
                                    pin=True,
                                )
                                if _mp_port <= 0 or not await _lsm_workers._port_alive(_mp_port):
                                    raise RuntimeError(f"Port {_mp_port} nicht erreichbar")
                                _worker_slots.append({
                                    "model": _mp_model,
                                    "port": _mp_port,
                                    "n_parallel": _mp_parallel,
                                    "num_ctx": _mp_ctx,
                                })
                                _worker_fail_reasons.pop(_mp_model, None)
                                yield await ctx.emit({
                                    "type": "status",
                                    "content": (
                                        f"  ✓ Worker-Recovery: {_mp_model} "
                                        f"(Port {_mp_port}, ctx={_mp_ctx}, parallel={_mp_parallel})"
                                    ),
                                })
                            except Exception as _dre:
                                _dr_reason = str(_dre)[:220]
                                _worker_fail_reasons[_mp_model] = f"deferred_load: {_dr_reason}"
                                yield await ctx.emit({
                                    "type": "worker_slot_failed",
                                    "phase": "deferred_load",
                                    "worker_index": int(_mp.get("worker_index") or 0),
                                    "model": _mp_model,
                                    "ctx": _mp_ctx,
                                    "parallel": _mp_parallel,
                                    "reason": _dr_reason,
                                })
                                yield await ctx.emit({
                                    "type": "status",
                                    "content": f"⚠️ Worker recovery failed: {_mp_model} — {_dr_reason}",
                                })

                    if _worker_slots:
                        _final_live: list[dict] = []
                        _seen_ports_live: set[int] = set()
                        for _ws_live in _worker_slots:
                            _ws_port_live = int(_ws_live.get("port", 0) or 0)
                            if _ws_port_live <= 0 or _ws_port_live in _seen_ports_live:
                                continue
                            # False-Negativ Problem wie Pass 2+3. 3×0.4s reichen.
                            # FIX-PASS4: 3×0.4s=1.2s → 5×0.5s=2.5s — angleichen an
                            _p4_ok = False
                            for _p4a in range(5):
                                if await _lsm_workers._port_alive(_ws_port_live):
                                    _p4_ok = True
                                    break
                                if _p4a < 4:
                                    await asyncio.sleep(0.5)
                            if _p4_ok:
                                _final_live.append(_ws_live)
                                _seen_ports_live.add(_ws_port_live)
                            else:
                                _ws_model_live = str(_ws_live.get("model") or "?")
                                _worker_fail_reasons[_ws_model_live] = (
                                    f"pass4_stale: Port {_ws_port_live} nach deferred load tot"
                                )
                                yield await ctx.emit({
                                    "type": "worker_slot_failed",
                                    "phase": "pass4_stale",
                                    "model": _ws_model_live,
                                    "port": _ws_port_live,
                                    "reason": "Port dead after deferred load",
                                })
                        _worker_slots = _final_live

                if _planned_workers:
                    _active_models_final = {str(_ws.get("model")) for _ws in _worker_slots}
                    _missing_final = []
                    for _pw in _planned_workers:
                        _pw_model = str(_pw.get("model") or "?")
                        if _pw_model in _active_models_final:
                            continue
                        _missing_final.append({
                            "model": _pw_model,
                            "reason": _worker_fail_reasons.get(_pw_model, "Worker not available"),
                        })

                    yield await ctx.emit({
                        "type": "worker_pool_state",
                        "phase": "final",
                        "target_workers": len(_planned_workers),
                        "active_workers": len(_worker_slots),
                        "missing": _missing_final,
                    })

                    if _missing_final:
                        _brief = "; ".join(
                            f"{m['model']}: {str(m.get('reason') or '')[:90]}"
                            for m in _missing_final[:2]
                        )
                        if len(_missing_final) > 2:
                            _brief += f"; +{len(_missing_final) - 2} weitere"
                        yield await ctx.emit({
                            "type": "status",
                            "content": (
                                f"ℹ️ Worker-Pool degradiert: {len(_worker_slots)}/{len(_planned_workers)} aktiv"
                                + (f" — {_brief}" if _brief else "")
                            ),
                        })
                    else:
                        yield await ctx.emit({
                            "type": "status",
                            "content": f"✅ Worker pool stable: {len(_worker_slots)}/{len(_planned_workers)} active",
                        })
            if _use_parallel and not _worker_slots:
                _use_parallel = False
                yield await ctx.emit({
                    "type": "status",
                    "content": "Parallel pre-explore active, but no workers started; falling back to sequential",
                })
                if _xport is None or _xport == 0:
                    _xport = next(
                        (_sl.port for _sl in _lsm2._slots if _sl.model == _xexplore_mdl and _sl.is_running),
                        None
                    )
                    if _xport is None:
                        try:
                            yield await ctx.emit({"type": "status",
                                "content": f"⏳ Pre-Explore (seq): loading {_xexplore_mdl} after worker flap…"})
                            _xport = await asyncio.wait_for(
                                _lsm2.ensure_loaded(_xexplore_mdl, num_ctx=_xopts.get("num_ctx", 4096)),
                                timeout=150.0,
                            )
                        except Exception as _reload_err:
                            logger.warning("Explorer reload after worker flap failed: %s", _reload_err)
                            _xport = None

            if _worker_slots:
                _workers_were_loaded = True
            _xq: asyncio.Queue = asyncio.Queue(maxsize=100)
            _xq_open: bool = True  # BUG-2 FIX: Flag — Consumer aktiv?

            async def _xemit_bridge(data: dict) -> str:
                _etype = data.get("type", "?")
                logger.warning("[BRIDGE] ctx.emit: type=%s label=%s", _etype, data.get("label", data.get("content", "")[:40]))
                s = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                if not _xq_open:
                    logger.warning("[BRIDGE] DROPPED — _xq_open=False")
                    return s
                try:
                    await asyncio.wait_for(_xq.put(s), timeout=2.0)
                except (asyncio.TimeoutError, asyncio.QueueFull):
                    logger.warning("[BRIDGE] DROPPED — queue full: type=%s", _etype)
                return s

            if _use_parallel:
                # ── PARALLEL: Baum in Partitionen aufteilen ───────────
                import math as _math  # MATH-IMPORT-FIX-356: _math.ceil in AUTO-PARTITION-COUNT-FIX
                _pmax_files  = int(ctx.settings.get("duo_partition_max_files", 30))

                # IDEAL-PART-SIZE-EARLY: Budget-Berechnung VOR partition_tree_async,
                _n_workers_hint   = len(_worker_slots) if _worker_slots else 1
                _slot_ctxs        = [int(_ws.get("num_ctx") or _ws.get("ctx") or _xctx) for _ws in (_worker_slots or [])]
                _min_slot_ctx     = min(_slot_ctxs) if _slot_ctxs else _xctx
                _slot_n_parallel  = max(1, int((_worker_slots or [{}])[0].get("n_parallel", 1) or 1))
                _pctx_eff_est     = max(2048, _min_slot_ctx // _slot_n_parallel)
                _guard_budget     = int(_pctx_eff_est * 3.5 * 0.82)
                _sys_overhead     = 4200   # Sys-Prompt + Tree + Contract-Write (chars)
                _round_overhead   = 900
                _pread_target     = 2500
                # 2500 → budget/overhead = 54577/3400 = 16 → min(duo_partition_max_files=12, 16) = 12.
                _budget_for_reads = max(0, _guard_budget - _sys_overhead)
                _ideal_part_size  = max(2, min(_pmax_files, int(_budget_for_reads / (_pread_target + _round_overhead))))
                _effective_max_files = min(_pmax_files, _ideal_part_size)

                _wmax, _wdepth = _window_params(ctx)
                _window_paths = await _get_analysis_window(_ws_str, _wmax, _wdepth)
                if _tree_ctx:
                    _partitions = await partition_tree_async(_tree_ctx,
                                                 max_files_per_partition=_effective_max_files,
                                                 workspace_root=_ws_str,
                                                 preselect_paths=_window_paths)
                else:
                    # PageRank-selektierten Analyse-Fenster bauen (voller Walk + Ranking,
                    import os as _os
                    _walk_parts: list[dict] = []
                    _IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv",
                                    "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}
                    if _window_paths:
                        _walk_parts = _partitions_from_window(_window_paths, _effective_max_files)
                    else:
                        try:
                            for _root, _dirs, _files in _os.walk(_ws_str):
                                _dirs[:] = [d for d in sorted(_dirs) if d not in _IGNORE_DIRS]
                                _rel = _os.path.relpath(_root, _ws_str).replace("\\", "/")
                                _rel = "" if _rel == "." else _rel
                                _code_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".go",
                                              ".rs", ".cpp", ".c", ".h", ".java", ".cs",
                                              ".rb", ".php", ".swift", ".kt", ".md", ".json",
                                              ".yaml", ".yml", ".toml", ".cfg", ".ini",
                                              ".html", ".css", ".scss", ".less", ".sh", ".bat",
                                              ".ps1", ".sql", ".r", ".lua", ".vim", ".el",
                                              ".dockerfile", ".makefile", ".cmake",
                                              ".txt", ".rst", ".tex", ".csv"}
                                _fps = [
                                    _os.path.join(_rel, f).replace("\\", "/").lstrip("/")
                                    for f in sorted(_files)
                                    if _os.path.splitext(f)[1].lower() in _code_exts
                                ]
                                if _fps:
                                    for _ci in range(0, max(1, len(_fps)), max(1, _effective_max_files)):
                                        _chunk = _fps[_ci:_ci + _effective_max_files]
                                        _lbl = (_rel or "__root__") + (f":sz{_ci//_effective_max_files+1}" if len(_fps) > _effective_max_files else "")
                                        _walk_parts.append({"paths": _chunk, "label": _lbl})
                        except Exception:
                            pass
                    _partitions = _walk_parts if _walk_parts else [{"paths": [], "catch_all": True, "label": "__root__"}]

                _root_parts = [
                    p for p in _partitions
                    if str(p.get("label","")).lower() == "__root__"
                ]
                for _rp in _root_parts:
                    if len(_rp.get("paths", [])) < 3:
                        _non_root = [p for p in _partitions if p is not _rp]
                        if _non_root:
                            _best = max(_non_root, key=lambda p: len(p.get("paths", [])))
                            _seen = set(_best.get("paths", []))
                            for _fp in _rp.get("paths", []):
                                if _fp not in _seen:
                                    _best.setdefault("paths", []).append(_fp)
                            _partitions = _non_root

                #   overhead  = ~1200 Token (System-Prompt + Tree + Contract-Write)
                #   ideal_files = (kv_tokens - overhead) / avg_file
                _total_files_hint = sum(len(p.get("paths", [])) for p in _partitions)
                _n_parts_cfg = max(
                    _n_workers_hint,
                    _math.ceil(_total_files_hint / max(1, _ideal_part_size)),
                )
                _n_parts_cfg = min(_n_parts_cfg, max(1, _total_files_hint))
                logger.debug(
                    "[pre_explore] ctx=%d n_par=%d guard=%d ideal_part=%d n_parts=%d total_files=%d",
                    _min_slot_ctx, _slot_n_parallel, _guard_budget, _ideal_part_size, _n_parts_cfg, _total_files_hint,
                )

                try:
                    _files_per_worker = max((len(p.get("paths", [])) for p in _partitions), default=0)
                    _file_based_timeout = int(_files_per_worker * 30 + 240)
                    _dynamic_timeout_real = max(480, _file_based_timeout)
                    _x_timeout_s = max(
                        int(ctx.settings.get("duo_pre_explore_timeout_seconds", 600) or 600),
                        _dynamic_timeout_real,
                    )
                    _x_timeout_s = max(60, min(3600, _x_timeout_s))
                except Exception:
                    pass
                if len(_partitions) > _n_parts_cfg:
                    _partitions = _merge_down(_partitions, _n_parts_cfg)

                if len(_partitions) < 2:
                    if not _partitions:
                        _use_parallel = False
                        yield await ctx.emit({"type": "parallel_cancelled",
                                          "reason": "No partitions — empty workspace"})
                    else:
                        _use_parallel = False
                        yield await ctx.emit({"type": "status",
                            "content": "🔍 Pre-Explore: 1 partition — single-worker mode"})
                else:
                    _shared = [p for p in _partitions if p["is_shared"]]
                    _normal_all = [p for p in _partitions if not p["is_shared"]]

                    _requested_parts = max(1, int(_n_parts_cfg or 1))
                    _normal_before_split = len(_normal_all)
                    _split_seq = 0

                    while _normal_all and len(_normal_all) < _requested_parts:
                        _split_done = False
                        _candidate_indices = sorted(
                            range(len(_normal_all)),
                            key=lambda _i: len(_normal_all[_i].get("paths", [])),
                            reverse=True,
                        )
                        for _largest_idx in _candidate_indices:
                            _src = _normal_all[_largest_idx]
                            _src_paths = list(dict.fromkeys(_src.get("paths", [])))
                            if len(_src_paths) <= 3:
                                continue
                            _a_paths, _b_paths = _split_paths_by_parent(_src_paths)
                            if not _a_paths or not _b_paths:
                                continue
                            _split_seq += 1
                            _base_label = str(_src.get("label", "partition"))
                            _a = {**_src, "label": f"{_base_label}-s{_split_seq}a", "paths": _a_paths, "is_shared": False}
                            _b = {**_src, "label": f"{_base_label}-s{_split_seq}b", "paths": _b_paths, "is_shared": False}
                            _normal_all[_largest_idx:_largest_idx + 1] = [_a, _b]
                            _split_done = True
                            break
                        if not _split_done:
                            break
                    if len(_normal_all) > _normal_before_split:
                        yield await ctx.emit({
                            "type": "status",
                            "content": (
                                f"⚙️ Pre-Explore Split: Partitionen {_normal_before_split}→{len(_normal_all)} "
                                f"(angefordert: {_requested_parts})"
                            ),
                        })

                    _worker_ports = {
                        int(_ws.get("port", 0) or 0)
                        for _ws in (_worker_slots or [])
                        if int(_ws.get("port", 0) or 0) > 0
                    }
                    _n_workers_eff = len(_worker_ports) if _worker_ports else max(1, len(_worker_slots or []))
                    _n_parts_eff = len(_normal_all)
                    if _n_parts_cfg < _n_parts_eff:
                        yield await ctx.emit({
                            "type": "status",
                            "content": (
                                f"⚙️ Queue-First aktiv: {_n_parts_eff} normale Partitionen in Queue "
                                f"(cfg={_n_parts_cfg}, aktive Worker={_n_workers_eff})"
                            ),
                        })
                    if _shared and _normal_all:
                        _shared_files: list[str] = []
                        for _sp in _shared:
                            _shared_files.extend(_sp.get("paths", []))
                        if _shared_files:
                            _n_norm = len(_normal_all)
                            for _sfi, _sfp in enumerate(_shared_files):
                                _target = _normal_all[_sfi % _n_norm]
                                _target.setdefault("paths", []).append(_sfp)
                            _shared = []
                    _partitions = _shared + list(_normal_all)

                    # DEDUP-BEFORE-SHAPE-FIX: Dedupe VOR pre_explore_partition_shape senden.
                    _seen_partition_paths: set[str] = set()
                    _dedup_partitions: list[dict] = []
                    _dedup_removed = 0
                    for _pp in _partitions:
                        _pp_unique: list[str] = []
                        for _fp in _pp.get("paths", []):
                            _norm_fp = str(_fp or "").replace("\\", "/").lower()
                            if not _norm_fp:
                                continue
                            if _norm_fp in _seen_partition_paths:
                                _dedup_removed += 1
                                continue
                            _seen_partition_paths.add(_norm_fp)
                            _pp_unique.append(_fp)
                        if _pp_unique:
                            _dedup_partitions.append({**_pp, "paths": _pp_unique})
                    _partitions = _dedup_partitions
                    if _dedup_removed > 0:
                        yield await ctx.emit({
                            "type": "status",
                            "content": f"🧹 Pre-Explore Dedupe: {_dedup_removed} doppelte Datei-Zuordnung(en) entfernt",
                        })

                    _worker_degraded = max(0, len(_planned_workers) - len(_worker_slots)) if _planned_workers else 0
                    _normal_all_dedup = [p for p in _partitions if not p.get("is_shared")]
                    yield await ctx.emit({
                        "type": "pre_explore_partition_shape",
                        "ctx.mode": "parallel",
                        "shared_count": len(_shared),
                        "normal_count": len(_normal_all_dedup),
                        "configured_partitions": int(_n_parts_cfg),
                        "effective_partitions": len(_partitions),
                        "queue_first": True,
                        "worker_target": len(_planned_workers) if _planned_workers else len(_worker_slots),
                        "worker_active": len(_worker_slots),
                        "worker_degraded": _worker_degraded,
                        "active_worker_ports": sorted(list(_worker_ports)),
                        "selected_labels": [str(p.get("label", "?")) for p in _partitions],
                    })

                    # AUTO-TUNE PARALLEL-KORREKTUR:
                    #
                    # QUEUE-FIRST-AUTO-TUNE-FIX 356: Im Queue-First-Modus (mehr Partitionen als
                    _n_workers_final = max(1, len(_worker_slots))
                    _parts_per_worker_final = max(1, _math.ceil(_n_parts_eff / _n_workers_final))
                    _queue_first_mode = _n_parts_eff > _n_workers_final
                    try:
                        _default_multi_worker_parallel
                    except NameError:
                        _default_multi_worker_parallel = 1
                    if not _queue_first_mode and _parts_per_worker_final != _default_multi_worker_parallel:
                        _default_multi_worker_parallel = _parts_per_worker_final
                        for _wsidx, _ws in enumerate(list(_worker_slots)):
                            if _ws.get("n_parallel", 1) < _parts_per_worker_final:
                                try:
                                    _ws_base = _ws["model"].rsplit("#", 1)[0]
                                    _ws_cfg_match = next(
                                        (c for c in _worker_cfgs if (c.get("model") or "").strip() == _ws_base), {}
                                    )
                                    _ws_ctx2 = int(_ws_cfg_match.get("ctx") or _xctx)
                                    # physischer Request-Overflow. Clamp auf 1 bei ctx < 8192.
                                    _at_n_parallel_clamped = (
                                        1 if _ws_ctx2 < 8192 else _parts_per_worker_final
                                    )
                                    _rport2 = await _lsm_workers.ensure_loaded(
                                        _ws["model"], num_ctx=_ws_ctx2,
                                        n_parallel=_at_n_parallel_clamped, pin=True,
                                    )
                                    _worker_slots[_wsidx] = {**_ws, "port": _rport2, "n_parallel": _at_n_parallel_clamped}
                                    _at_parallel_note = " [ctx-clamped→1]" if _at_n_parallel_clamped < _parts_per_worker_final else ""
                                    yield await ctx.emit({"type": "status",
                                        "content": f"  ↑ Worker {_ws['model']} parallel {_ws.get('n_parallel',1)}→{_at_n_parallel_clamped} (auto-tune{_at_parallel_note})"})
                                except Exception as _at_err:
                                    logger.warning("Auto-tune parallel reload failed (%s): %s", _ws["model"], _at_err)

                    _pre_files_total = sum(len(p.get("paths", [])) for p in _partitions)
                    _pre_files_unique = len({
                        _fp
                        for _p in _partitions
                        for _fp in _p.get("paths", [])
                    })
                    yield await ctx.emit({
                        "type": "pre_explore_info",
                        "ctx.mode": "parallel",
                        "n_partitions": len(_partitions),
                        "n_files_total": _pre_files_total,
                        "n_files_unique": _pre_files_unique,
                        "workers": [
                            {
                                "model": str(_ws.get("model", "?")),
                                "port": int(_ws.get("port", 0) or 0),
                                "n_parallel": int(_ws.get("n_parallel", 1) or 1),
                            }
                            for _ws in _worker_slots
                        ],
                        "labels": [str(p.get("label", "?")) for p in _partitions],
                    })

                    yield await ctx.emit({"type": "status",
                        "content": f"🔀 Parallel Pre-Explore: {len(_partitions)} Partitionen "
                                   f"({', '.join(p['label'] for p in _partitions)})"})

                    async def _xparallel_task():
                        _task_ok = False
                        try:
                            _result = await asyncio.wait_for(
                                _run_parallel_pre_explore(
                                    exec_mdl          = _xexplore_mdl,
                                    worker_slots      = _worker_slots,
                                    partitions        = _partitions,
                                    xtools            = _xtools,
                                    xopts             = {**_xopts, "_preexplore_max_tools": _xmax},
                                    xport             = _xport,
                                    workspace         = _ws_str,
                                    tree_ctx          = _tree_ctx,
                                    user_input        = ctx.user_input,
                                    xctx_chars_limit  = _XCTX_CHARS_LIMIT,
                                    xmsg_hard_cap     = _XMSG_HARD_CAP,
                                    aborted_fn        = ctx.aborted,
                                    step_skipped_fn   = ctx.step_skipped,
                                    emit_fn           = _xemit_bridge,
                                    websearch_enabled = False,
                                    searched_queries  = _duo_seen_web_queries,
                                    preload_fn        = None,
                                    settings_dict     = ctx.settings,
                                    thinking_override = _x_thinking_override,
                                ),
                            timeout=_x_timeout_s,
                            )
                            _task_ok = True
                            return _result
                        finally:
                            for _ws_unpin in _worker_slots:
                                try:
                                    await _lsm_workers.unpin(_ws_unpin["model"])
                                except Exception:
                                    pass
                            if not _task_ok:
                                try:
                                    _exec_base_fin = exec_mdl.rsplit("#", 1)[0] if "#" in exec_mdl else exec_mdl
                                    for _ws_fin in _worker_slots:
                                        _wm_fin = str(_ws_fin.get("model", "") or "")
                                        _wm_base_fin = _wm_fin.rsplit("#", 1)[0] if "#" in _wm_fin else _wm_fin
                                        if _wm_base_fin != _exec_base_fin:
                                            await _lsm_workers.evict(_wm_fin)
                                except Exception as _evict_exc:
                                    logger.warning("Worker-evict after parallel failure failed: %s", _evict_exc)
                            await _xq.put(None)

                    if _partitions and _ws_str and _static_map_task is None:
                        try:
                            from hive_functions.static_repomap import build_static_repomap
                            from hive_functions.ctx_utils import derive_static_map_budget
                            _map_budget = derive_static_map_budget(
                                state.get("_exec_ctx"),
                                ctx.settings.get("duo_planner_ctx_target", 0),
                                ctx.settings.get("duo_static_map_chars", 0),
                            )
                            _static_map_task = asyncio.create_task(
                                build_static_repomap(_ws_str, _partitions, char_budget=_map_budget)
                            )
                        except Exception as _sm_init:
                            logger.warning("[STATIC-REPO-MAP] init failed: %s", _sm_init)

                    _xtask = asyncio.create_task(_xparallel_task())
                    try:
                        while True:
                            _xitem = await _xq.get()
                            if _xitem is None:
                                break
                            yield _xitem
                        while not _xq.empty():
                            _xdrain = _xq.get_nowait()
                            if _xdrain is not None:
                                yield _xdrain
                    except GeneratorExit:
                        _xtask.cancel()
                        raise
                    _xparallel_results: list = []
                    _xmsgs = []            # pre-init — prevents NameError on timeout
                    try:
                        _explore_ctx, _, _xparallel_results, _xmsgs = await _xtask
                        logger.warning("[PRE-EXPLORE-PARALLEL-RESULT] _explore_ctx=%r, _xmsgs=%d items",
                                       (_explore_ctx or "")[:120], len(_xmsgs) if isinstance(_xmsgs, list) else 0)
                    except BaseException as _pxe:
                        if isinstance(_pxe, (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                            raise
                        import traceback as _tb_pxe
                        _pxe_dur = time.time() - _xexplore_t
                        _pxe_msg = str(_pxe) or "(empty — asyncio.TimeoutError has no default message)"
                        _pxe_n_parts = len(_partitions) if _partitions else 0
                        logger.warning(
                            "[PRE-EXPLORE-PARALLEL-EXCEPTION] type=%s duration=%.0fs timeout=%ds "
                            "partitions=%d workers=%d model=%s msg=%s\n%s",
                            type(_pxe).__name__, _pxe_dur, _x_timeout_s,
                            _pxe_n_parts, len(_worker_slots), _xexplore_mdl,
                            _pxe_msg, _tb_pxe.format_exc(),
                        )
                        if isinstance(_pxe, asyncio.TimeoutError):
                            _explore_ctx = (
                                f"[Pre-Explore timeout guard after {_x_timeout_s}s — "
                                "using partial context only]"
                            )
                            # S2: Partial recovery — build minimal explore_ctx from partition metadata
                            # when workers timed out before calling write_contract.
                            if _partitions:
                                _pctx_lines = ["## Codebase Architecture Map (partial — timeout recovery)\n"]
                                for _p in _partitions:
                                    _plabel = _p.get("label", "?")
                                    _ppaths = _p.get("paths", [])
                                    _pctx_lines.append(f"### [3] {_plabel} ⚡ TOUCHED")
                                    _pctx_lines.append(f"  Role: {_plabel} partition (partial — worker timed out)")
                                    _pctx_lines.append(f"  Exports: (unknown)")
                                    _pctx_lines.append(f"  Files: {len(_ppaths)}")
                                    _pctx_lines.append("")
                                    _pctx_lines.append("```toml")
                                    _pctx_lines.append("[contract]")
                                    _pctx_lines.append(f'partition = "{_plabel}"')
                                    _pctx_lines.append(f'role = "{_plabel} partition (partial — worker timed out)"')
                                    _pctx_lines.append(f'files_read = {json.dumps([str(Path(fp).as_posix()) for fp in _ppaths[:20]])}')
                                    _pctx_lines.append('exports = []')
                                    _pctx_lines.append('touched_by_task = "yes"')
                                    _pctx_lines.append('complexity_score = 0.5')
                                    _pctx_lines.append("```")
                                    _pctx_lines.append("")
                                _explore_ctx = _explore_ctx + "\n\n" + "\n".join(_pctx_lines)
                                for _p in _partitions:
                                    for _fp in _p.get("paths", []):
                                        _touched_paths.add(str(_fp).replace("\\", "/").lower())
                                logger.info(
                                    "[PRE-EXPLORE] Timeout recovery: built partial explore_ctx from %d partition(s)",
                                    len(_partitions),
                                )
                            yield await ctx.emit({
                                "type": "status",
                                "content": (
                                    f"⏱️ Parallel Pre-Explore time budget reached ({_x_timeout_s}s) — "
                                    "continuing with partial context"
                                ),
                            })
                            _xmsgs = _xmsgs if isinstance(_xmsgs, list) else []
                        else:
                            logger.warning(
                                "Parallel pre-explore failed (%s) - falling back to sequential.",
                                _pxe,
                            )
                            yield await ctx.emit({
                                "type": "status",
                                "content": f"⚠️ Parallel Pre-Explore fehlgeschlagen ({str(_pxe)[:90]}) — fallback: sequenziell",
                            })

                            import socket as _sock_fb
                            _fb_model = _xexplore_mdl
                            if _fb_model == exec_mdl:
                                _small_fallbacks = ["lfm2.5:8b-a1b", "qwen3.5:2b-ud", "qwen3.5:2b-d", "qwen3.5:2b", "qwen3.5:0.8b", "granite4:1b"]
                                for _sf in _small_fallbacks:
                                    if _sf in ctx.models_cache:
                                        _fb_model = _sf
                                        break

                            _fb_port = 0
                            for _wsfb in _worker_slots:
                                _candidate_port = int(_wsfb.get("port", 0) or 0)
                                if _candidate_port <= 0:
                                    continue
                                try:
                                    with _sock_fb.create_connection(("127.0.0.1", _candidate_port), timeout=0.5):
                                        _fb_model = str(_wsfb.get("model") or _xexplore_mdl)
                                        _fb_port = _candidate_port
                                        break
                                except OSError:
                                    continue

                            if _fb_port <= 0:
                                _fb_model = _xexplore_mdl
                                _fb_port = int(_xport or 0)

                            if _fb_port <= 0:
                                _fb_port = await _lsm2.ensure_loaded(
                                _fb_model,
                                num_ctx=_xopts.get("num_ctx", 4096),
                                n_parallel=2)


                            async with httpx.AsyncClient(
                                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                                timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=5.0),
                            ) as _xexplore_client_fb:
                                if _fb_port is None or _fb_port == 0:
                                    _explore_error = f"Pre-explore: no valid port for {_fb_model}"
                                    logger.warning("[PRE-EXPLORE-FALLBACK] %s", _explore_error)
                                    _explore_ctx = ""
                                    yield await ctx.emit({"type": "status", "content": f"⛔ Pre-Explore: model {_fb_model.split(':')[0]} has no valid port — skipped."})
                                    return
                                _explore_ctx, _, _, _xmsgs = await run_pre_explore(
                                    model           = _fb_model,
                                    port            = _fb_port,
                                    partitions      = _partitions,
                                    workspace       = _ws_str,
                                    tree_ctx        = _tree_ctx,
                                    user_input      = ctx.user_input,
                                    worker_slots    = None,
                                    parallel        = False,
                                    max_tool_rounds = _xmax,
                                    pctx            = int(_xopts.get("num_ctx", 0)),
                                    aborted_fn      = ctx.aborted,
                                    emit_fn         = _xemit_bridge,
                                    thinking_override = _x_thinking_override,
                                    llm_read_timeout = float(_xopts.get("_llm_read_timeout_s", 300.0) or 300.0),
                                )
                    _xq_open = False
                    _parallel_explore_ran = True

            #
            #
            if not _use_parallel and (_xport is None or _xport == 0) and not _parallel_explore_ran:
                if _worker_slots:
                    _first_worker = _worker_slots[0]
                    _xport = int(_first_worker.get("port") or 0)
                    _xexplore_mdl = str(_first_worker.get("model") or _xexplore_mdl)
                    yield await ctx.emit({"type": "status",
                        "content": f"🔍 Pre-Explore (seq): using worker {_xexplore_mdl} (port {_xport})"})
                elif _xexplore_mdl != exec_mdl:
                    try:
                        yield await ctx.emit({"type": "status",
                            "content": f"⏳ Pre-Explore (seq): lade {_xexplore_mdl}…"})
                        _xport = await asyncio.wait_for(
                            _lsm2.ensure_loaded(_xexplore_mdl, num_ctx=_xopts.get("num_ctx", 4096)),
                            timeout=150.0,
                        )
                    except asyncio.TimeoutError:
                        yield await ctx.emit({"type": "status",
                            "content": f"⛔ Pre-Explore: {_xexplore_mdl} not responding — skipped."})
                        try:
                            await _lsm2.evict(_xexplore_mdl)
                        except Exception:
                            pass
                        _xport = None
                    except Exception as _lazy_err:
                        yield await ctx.emit({"type": "status",
                            "content": f"⚠️ Pre-Explore: model load failed ({str(_lazy_err)[:80]}) — skipped."})
                        try:
                            await _lsm2.evict(_xexplore_mdl)
                        except Exception:
                            pass
                        _xport = None
                else:
                    if _duo_pre_explore_user_explicit and _exec_tc:
                        try:
                            yield await ctx.emit({"type": "status",
                                "content": f"⏳ Pre-Explore (seq): loading {_xexplore_mdl} for user-explicit explore…"})
                            _xport = await asyncio.wait_for(
                                _lsm2.ensure_loaded(_xexplore_mdl, num_ctx=_xopts.get("num_ctx", 4096)),
                                timeout=150.0,
                            )
                        except asyncio.TimeoutError:
                            yield await ctx.emit({"type": "status",
                                "content": f"⛔ Pre-Explore: {_xexplore_mdl.split(':')[0]} not responding after 150s — skipped."})
                            _xport = None
                        except Exception as _zone1_err:
                            yield await ctx.emit({"type": "status",
                                "content": f"⚠️ Pre-Explore: model load failed ({str(_zone1_err)[:80]}) — skipped."})
                            _xport = None
                    elif _duo_pre_explore_user_explicit and not _exec_tc:
                        yield await ctx.emit({"type": "status",
                            "content": (
                                f"⚠️ Pre-Explore skipped: {_xexplore_mdl.split(':')[0]} has no "
                                "tool-call support. Configure an explorer override with a tool-call model "
                                "(e.g. qwen3:8b or qwen2.5:7b)."
                            )})
                        _xport = None
                    else:
                        yield await ctx.emit({"type": "status",
                            "content": "ℹ️ Pre-explore skipped (sequential, no explorer override — zone-1 rule)."})
                        _xport = None

            if not _use_parallel and _xport is not None and _xport > 0:
                _workers_were_loaded = True
                yield await ctx.emit({
                    "type": "pre_explore_info",
                    "ctx.mode": "sequential",
                    "n_partitions": 1,
                    "workers": [
                        {
                            "model": str(_xexplore_mdl),
                            "port": int(_xport or 0),
                            "n_parallel": int(
                                (_worker_slots[0].get("n_parallel", 1) if _worker_slots else 1) or 1
                            ),
                        }
                    ],
                    "labels": ["workspace"],
                })

                _xparallel_results: list = []
                # SEQ-PARTITION-CLOSURE-FIX: locals().get() in _xloop_task (Closure)
                if not _partitions:
                    _seq_parts: list[dict] = []
                    _seq_max_files = _effective_max_files or 10
                    _seq_window: list[str] = []
                    try:
                        _wmax_s, _wdepth_s = _window_params(ctx)
                        _seq_window = await _get_analysis_window(_ws_str, _wmax_s, _wdepth_s)
                    except Exception:
                        _seq_window = []
                    if _tree_ctx:
                        try:
                            _seq_parts = await partition_tree_async(_tree_ctx,
                                                max_files_per_partition=max(2, _seq_max_files),
                                                workspace_root=_ws_str,
                                                preselect_paths=_seq_window)
                        except Exception:
                            _seq_parts = []
                    if not _seq_parts and _seq_window:
                        _seq_parts = _partitions_from_window(_seq_window, _seq_max_files)
                    if not _seq_parts:
                        import os as _os2
                        _IGNORE_DIRS2 = {".git", "__pycache__", "node_modules", ".venv",
                                        "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}
                        _code_exts2 = {".py", ".js", ".ts", ".jsx", ".tsx", ".go",
                                      ".rs", ".cpp", ".c", ".h", ".java", ".cs",
                                      ".rb", ".php", ".swift", ".kt", ".md", ".json",
                                      ".yaml", ".yml", ".toml", ".cfg", ".ini",
                                      ".html", ".css", ".scss", ".less", ".sh", ".bat",
                                      ".ps1", ".sql", ".r", ".lua", ".vim", ".el",
                                      ".dockerfile", ".makefile", ".cmake",
                                      ".txt", ".rst", ".tex", ".csv"}
                        try:
                            for _root2, _dirs2, _files2 in _os2.walk(_ws_str):
                                _dirs2[:] = [d for d in sorted(_dirs2) if d not in _IGNORE_DIRS2]
                                _rel2 = _os2.relpath(_root2, _ws_str).replace("\\", "/")
                                _rel2 = "" if _rel2 == "." else _rel2
                                _fps2 = [
                                    _os2.join(_rel2, f).replace("\\", "/").lstrip("/")
                                    for f in sorted(_files2)
                                    if _os2.splitext(f)[1].lower() in _code_exts2
                                ]
                                if _fps2:
                                    _seq_parts.append({"paths": _fps2, "label": _rel2 or "__root__"})
                        except Exception:
                            pass
                    _partitions = _seq_parts if _seq_parts else [{"paths": [], "catch_all": True, "label": "__root__"}]
                async def _xloop_task():
                    try:
                        if _xport is None or _xport == 0:
                            _explore_error = f"Pre-explore: no valid port for {_xexplore_mdl}"
                            logger.warning("[PRE-EXPLORE-FALLBACK] %s", _explore_error)
                            _explore_ctx = ""
                            await _xq.put(await ctx.emit({"type": "status", "content": f"⛔ Pre-Explore: model {_xexplore_mdl.split(':')[0]} has no valid port — skipped."}))
                            return "", [], []
                        try:
                            _n_parts_seq = len(_partitions)
                            _total_files_seq = sum(len(p.get("paths", [])) for p in _partitions)
                            _per_file_s = float(ctx.settings.get("duo_pre_explore_timeout_per_file_s", 20.0) or 20.0)
                            _seq_tm = max(
                                _x_timeout_s,
                                int(_total_files_seq * _per_file_s + 120),
                                int(_n_parts_seq * 90 + 60),
                            )
                            _seq_timeout_s = float(max(60, min(3600, _seq_tm)))
                        except Exception:
                            _seq_timeout_s = float(_x_timeout_s)
                        _seq_ctx, _seq_results, _seq_contracts, _seq_msgs = await asyncio.wait_for(
                            run_pre_explore(
                                model           = _xexplore_mdl,
                                port            = _xport,
                                partitions      = _partitions,
                                workspace       = _ws_str,
                                tree_ctx        = _tree_ctx,
                                user_input      = ctx.user_input,
                                worker_slots    = None,   # Single-Worker
                                parallel        = False,
                                max_tool_rounds = _xmax,
                                pctx            = int(_xopts.get("num_ctx", 0)),
                                aborted_fn      = ctx.aborted,
                                emit_fn         = _xemit_bridge,
                                thinking_override = _x_thinking_override,
                                llm_read_timeout = float(_xopts.get("_llm_read_timeout_s", 300.0) or 300.0),
                            ),
                            timeout=_seq_timeout_s,
                        )
                        return _seq_ctx, _seq_results, _seq_contracts, _seq_msgs
                    finally:
                        await _xq.put(None)

                if _partitions and _ws_str and _static_map_task is None:
                    try:
                        from hive_functions.static_repomap import build_static_repomap
                        from hive_functions.ctx_utils import derive_static_map_budget
                        _map_budget = derive_static_map_budget(
                            state.get("_exec_ctx"),
                            ctx.settings.get("duo_planner_ctx_target", 0),
                            ctx.settings.get("duo_static_map_chars", 0),
                        )
                        _static_map_task = asyncio.create_task(
                            build_static_repomap(_ws_str, _partitions, char_budget=_map_budget)
                        )
                    except Exception as _sm_init:
                        logger.warning("[STATIC-REPO-MAP] init failed: %s", _sm_init)

                _xtask = asyncio.create_task(_xloop_task())
                try:
                    while True:
                        _xitem = await _xq.get()
                        if _xitem is None:
                            break
                        yield _xitem
                    while not _xq.empty():
                        _xdrain = _xq.get_nowait()
                        if _xdrain is not None:
                            yield _xdrain
                except GeneratorExit:
                    _xtask.cancel()
                    raise
                _xmsgs = []
                _xparallel_results = []    # pre-init — prevents NameError on timeout
                try:
                    _explore_ctx, _, _xparallel_results, _xmsgs = await _xtask
                except BaseException as _xe_task:
                    if isinstance(_xe_task, (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                        raise
                    if isinstance(_xe_task, asyncio.TimeoutError):
                        _explore_ctx = f"[Pre-Explore timeout guard after {_x_timeout_s}s]"
                        if _partitions:
                            for _p in _partitions:
                                for _fp in _p.get("paths", []):
                                    _touched_paths.add(
                                        str(_fp).replace("\\", "/").lower()
                                    )
                            logger.info(
                                "[PRE-EXPLORE] Timeout recovery: marked "
                                "%d files as touched from %d partition(s)",
                                len(_touched_paths), len(_partitions),
                            )
                        yield await ctx.emit({
                            "type": "status",
                            "content": f"⏱️ Pre-Explore time budget reached ({_x_timeout_s}s) — continuing with partial context",
                        })
                    else:
                        import traceback as _tb2
                        logger.error("[PRE-EXPLORE-TASK-ERROR] %s\n%s", _xe_task, _tb2.format_exc())
                        _explore_error = f"Pre-explore failed: {str(_xe_task)[:80]}"
                        logger.warning("[PRE-EXPLORE-FALLBACK] %s — coder will start with empty explore_ctx", _explore_error)
                        _explore_ctx = ""  # empty — no error string, avoids cache-pollution
                        yield await ctx.emit({"type": "status", "content": f"⚠️ Pre-Explore task error: {str(_xe_task)[:120]}"})
                finally:
                    _xq_open = False
        except BaseException as _xe:
            if isinstance(_xe, (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            _explore_error = f"Pre-explore exception: {str(_xe)[:80]}"
            logger.error("[PRE-EXPLORE-FALLBACK] %s", _explore_error)
            _explore_ctx = ""
            import traceback as _tb
            logger.error("[PRE-EXPLORE-ERROR] Exception in try block: %s\n%s", _xe, _tb.format_exc())
            try:
                ctx.phase_timer.end("pre_explore", status="error")
            except Exception:
                pass
            yield await ctx.emit({"type": "status", "content": f"⚠️ Pre-Explore error: {str(_xe)[:120]}"})
        finally:
            # On success the dedicated "unified evict" block below already
            # frees VRAM for the planner/coder in a controlled sequence. A full
            # force_kill_all here on every success was redundant and forced
            # reloads of every model. Keep it as an error-path safety net so
            # zombie workers from a failed explore are still cleaned up.
            if locals().get("_explore_error"):
                try:
                    from backend.llama_compat import force_kill_all
                    await force_kill_all()
                    logger.info("[PRE-EXPLORE-CLEANUP] force_kill_all fired (pre-explore failed)")
                except Exception as _fke:
                    logger.warning("[PRE-EXPLORE-CLEANUP] force_kill_all failed: %s", _fke)

        state.update({
            "_static_map_task": _static_map_task,
            "_explore_ctx": _explore_ctx,
            "_tree_ctx": _tree_ctx,
            "_ws_str": _ws_str,
            "_worker_slots": _worker_slots,
            "_xexplore_mdl": _xexplore_mdl,
            "_use_parallel": _use_parallel,
            "_partitions": _partitions,
            "_xparallel_results": _xparallel_results,
            "_xexplore_t": _xexplore_t,
            "_duo_pinned": _duo_pinned,
            "_ck": _ck,
            "_pre_explore_msgs": _pre_explore_msgs,
            "_chat_ctx_loaded": _chat_ctx_loaded,
            "_xmsgs": _xmsgs,
        })
        async for _ev in _phase_pre_explore_finalize(ctx, state):
            yield _ev
        _explore_ctx = state["_explore_ctx"]
        _pre_explore_msgs = state["_pre_explore_msgs"]
        _touched_paths = state["_touched_paths"]
        _plan_tracker = state["_plan_tracker"]
        _contracts_raw = state["_contracts_raw"]
        _duo_pinned = state["_duo_pinned"]


    from utils.workspace_resolve import (
        sync_env_workspace as _ws_final_sync,
        save_last_workspace as _ws_final_save,
    )
    if _ws_str:
        _ws_final_sync(_ws_str)
        _ws_final_save(_ws_str)

    state["_ws_str"] = _ws_str
    state["_explore_ctx"] = _explore_ctx
    state["_tree_ctx"] = _tree_ctx
    state["_resume_data"] = _resume_data
    state["_plan_tracker"] = _plan_tracker
    state["_contracts_raw"] = _contracts_raw
    state["_pre_explore_msgs"] = _pre_explore_msgs
    state["_use_parallel"] = _use_parallel
    state["_worker_slots"] = _worker_slots
    state["_workers_were_loaded"] = _workers_were_loaded
    state["_xexplore_mdl"] = _xexplore_mdl
    state["_touched_paths"] = _touched_paths