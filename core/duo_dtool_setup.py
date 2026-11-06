"""Duo dtool setup (2026-11-05 split 2.5).

Runs when a round is a tool round: coder tool-payload budget (ctx /
max_tokens), prefill-cost log, read-tool timeout, pass-explore-files
bridge message, read-guard pre-population (pre-explore + persisted
.context.json), explore-fallback and goal-pin, and the `_dtool_msgs`
assembly. Verbatim code motion — no logic changes. Outputs flow back
through `ps`; the runner consumes them for the tool payload.
"""
from __future__ import annotations

import hashlib
import json
import logging

from context.chat import _load_chat_context
from core.duo.preflight import (
    _build_dtool_base, _build_os_hint, _build_touched_context,
    _inject_plan_into_coder_msgs,
)
from core.duo_helpers import (
    _resolve_tool_read_timeout_seconds, _should_escalate_pass_files,
)
from hive_functions.ctx_utils import budget_explore_window
from hive_functions.tree_scout import TREE_HEADER_PREFIX
from utils.file import normalize_tool_path as _normalize_tool_path
from utils.tool import parse_tool_args as _parse_tool_args

logger = logging.getLogger("hivemind.duo")


async def _duo_dtool_setup(ctx, ps):
    _di = ps._di
    coder_mdl = ps.carry["coder_mdl"]
    _duo_coder_sys = ps.carry["_duo_coder_sys"]
    _duo_coder_tok = ps.carry["_duo_coder_tok"]
    _duo_runtime_profile = ps.carry["_duo_runtime_profile"]
    _coder_ctx_eff = ps.carry["_coder_ctx_eff"]
    _coder_map_budget = ps.carry["_coder_map_budget"]
    _plan_result = ps.carry.get("_plan_result")
    _coder_caps = ps._coder_caps
    _coder_msgs = ps._coder_msgs
    _explore_ctx = ps._explore_ctx
    _pre_explore_msgs = ps._pre_explore_msgs
    _contracts_raw = ps._contracts_raw
    _touched_paths = ps._touched_paths
    _tree_ctx = ps._tree_ctx
    _is_first_outer_round = ps._is_first_outer_round
    _ws_str = ps._ws_str
    _touched_block_persisted = ps._touched_block_persisted

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
    except (ImportError, OSError, ValueError, TypeError) as _wb_err:
        logger.debug("[CODER-BUDGET] write-budget apply skipped: %s", _wb_err)
    # PREFILL-COST (2026-10-27, live slow run): the coder ctx is the
    # cold-prefill cost. Measured on this box: decode 37-48 t/s but
    # prefill ~100 t/s, so ctx 85000 costs up to 15 min per cache
    # miss (a 38k prompt measured 420 s). Say it at run start.
    _cold_ctx = int(_dtool_ctx or 0)
    _cold_mdl = str(
        ((ctx.settings.get("agents") or {}).get("duo_coder") or {})
        .get("model") or "")
    try:
        from core.budgets import prefill_tps_for as _ptps_log
        _rate_log = max(40.0, min(2000.0, float(
            _ptps_log(_cold_mdl) or 0)))
    except (ImportError, TypeError, ValueError):
        _rate_log = 100.0
    if _cold_ctx >= 16384:
        logger.warning(
            "[PREFILL-COST] coder ctx=%d @%.0f t/s prefill: a cold "
            "prefill costs about %.0fs. Every prefix-cache miss "
            "(compression, an edited early message) pays that again.",
            _cold_ctx, _rate_log, _cold_ctx / _rate_log)
    _tool_read_timeout_s = _resolve_tool_read_timeout_seconds(
        num_ctx=_cold_ctx,
        model=_cold_mdl,
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
                f"- write_file(path, content) writes a file's FULL content — use it to create a NEW file "
                f"or to overwrite an existing file completely.\n"
                f"- To change only PART of an existing file, use edit_file with EXACT text:\n"
                f"    edit_file(path=<file>, old_text=<the exact current code copied verbatim>, new_text=<the replacement>)\n"
                f"  old_text must match the file exactly (whitespace included) and appear exactly once.\n"
                f"- If a tool returns READ_REQUIRED: call read_file once on that path, then retry.\n"
                f"- Do NOT call list_dir or find_files on directories already shown above.\n"
                f"- Do not run tests before your first write/edit in this run; "
                f"run them only after changing code. For static assets "
                f"(HTML, CSS, Markdown, JSON, SVG) reading the file back is enough - "
                f"do NOT run the project test suite for those."
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
            _plan_result.plan_content if (_plan_result and _plan_result.plan_content) else "",
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
            # GOAL-PIN LIST GUARD (2026-10-01): message 1 can carry
            # OpenAI content parts (raw images) — never str() them
            # (would corrupt the parts into repr garbage); prepend
            # the pin as an extra text part instead.
            if isinstance(_existing_content, list):
                _dtool_msgs[1] = {
                    **_dtool_msgs[1],
                    "content": [{"type": "text",
                                 "text": _goal_pin_msg["content"]}]
                               + _existing_content
                }
            else:
                _dtool_msgs[1] = {
                    **_dtool_msgs[1],
                    "content": _goal_pin_msg["content"] + "\n\n" + str(_existing_content)
                }
        else:
            _dtool_msgs = (
                [_dtool_msgs[0], _goal_pin_msg]
                + _dtool_msgs[1:]
            )

    # ── sync back: everything the runner consumes after this block ──
    ps._dtool_msgs = _dtool_msgs
    ps._dtool_ctx = _dtool_ctx
    ps._dtool_opts = _dtool_opts
    ps._dtool_sys_eff = _dtool_sys_eff
    ps._tool_read_timeout_s = _tool_read_timeout_s
    ps._goal_summary = _goal_summary
    ps._goal_pin_msg = _goal_pin_msg
    ps._touched_block_persisted = _touched_block_persisted
    ps.carry["_coder_map_budget"] = _coder_map_budget
