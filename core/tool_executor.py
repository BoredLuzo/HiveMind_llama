# -*- coding: utf-8 -*-
"""Shared tool-call execution loop extracted from duo_runner.py (~430L inline).

Handles: arg parsing, _run_inline_tool dispatch, error injection (too-large,
file-exists, invalid-format), loop detection, reactive thinking, file-change
tracking, context LRU, ask_user/web_search special cases, Until-Finished
stuck detection, user abort check.

Does NOT handle: context compression, POST+retry, plan tracking, exception
recovery — those remain in duo_runner.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

from tools.runner import _run_inline_tool
from utils.tool import parse_tool_args as _parse_tool_args, run_bash_failed as _run_bash_failed
from sse.events import make_tool_call_event as _make_tool_call_event, make_tool_result_event as _make_tool_result_event
from tools.errors import tool_call_failed as _tool_call_failed, tool_error_has_code as _tool_error_has_code, tool_error_response as _tool_error_response
from core.agentic_duo_state import DuoRoundState
from core.tool_exec_helpers import (
    ToolRoundState,
    _SYS_PREFIX,
    _prefetch_readonly_tools, _warn_duplicate_write_targets, _track_focus_path,
    _note_successful_write, _update_read_ladder,
    _handle_too_large, _inject_tool_error_hints,
    _handle_ask_user, _execute_one_tool, _maybe_activate_reactive_think,
    _run_bash_fail_fix_pass_insight, _patch_file_fallback_hint,
    _read_required_and_python_hints, _unknown_error_hint,
    _track_file_changes, _register_context_lru, _track_edit_noop,
    _recovery_saturated,
)

_logger = logging.getLogger("tool_executor")


# ── task_complete block/allow ladder (was 5 near-identical inline copies) ──
_TC_ALLOWED_MSG = ("[task_complete ALLOWED] Accepted with status=blocked "
                   "after repeated attempts — proceeding.")

def _task_complete_ladder(trs, dtool_msgs, result, allow_on_blocked: bool,
                          blocked_msg: str, allowed_msg: str | None = None) -> str:
    """Count a blocked task_complete attempt. Returns:
    'block'       -> feedback injected; caller continues the round loop
    'allow_break' -> accepted (3rd attempt + tc_blocked); caller breaks
    'allow'       -> accepted without tc_blocked; caller keeps its own flow
    """
    trs.task_complete_blocked_count[0] += 1
    if trs.task_complete_blocked_count[0] < 3:
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX + blocked_msg)})
        return "block"
    if allowed_msg is not None:
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX + allowed_msg)})
    result.task_complete_called = True
    return "allow_break" if allow_on_blocked else "allow"

def _render_tool_menu(tool_mode: str, duo_ws: bool, max_items: int = 30) -> str:
    """BASH-LOOP-REINJECT (2026-09-02): build a compact list of the tools that
    are actually available in this mode, so a bash-looping model is reminded of
    its full toolset instead of being aborted."""
    try:
        from tools.definitions import _get_inline_tools as _gt
        _tools = _gt(include_websearch=duo_ws, mode=tool_mode) or []
    except Exception:
        return ""
    _lines = []
    for _t in _tools[:max_items]:
        try:
            _fn = _t.get("function", {})
            _name = _fn.get("name", "")
            _desc = str(_fn.get("description", "") or "").replace("\n", " ").strip()
            if not _name:
                continue
            if _desc:
                # first meaningful sentence as one-line purpose
                _first = re.split(r"(?<=[.!?])\s", _desc)[0].strip()
                if len(_first) > 160:
                    _first = _first[:160] + "…"
                _lines.append(f"  - {_name}: {_first}")
            else:
                _lines.append(f"  - {_name}")
        except Exception:
            continue
    return "\n".join(_lines)


# ═══════════════════════════════════════════════════════════════════════════
#  Hooks  —  callbacks duo_runner must provide
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ToolExecHooks:
    """Callbacks wired by duo_runner to inject context-dependent behaviour."""
    emit: Callable[[dict], Awaitable[None]]
    is_aborted: Callable[[str], bool]                     # (chat_id) -> bool
    on_tool_result: Callable[[str, dict, str], Awaitable[bool | None]] | None = None
    # ^ (tool_name, args, result) -> return True to break loop (stuck detection)
    remember_insight: Callable[..., Awaitable] | None = None
    evict_model: Callable[[str], Awaitable] | None = None


# ═══════════════════════════════════════════════════════════════════════════
#  Tool-exec result
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ToolExecResult:
    loop_detected: bool = False
    duo_timed_out: bool = False
    file_changes: dict = field(default_factory=dict)
    verify_mutation_serial: int = 0
    verify_last_ok_serial: int = 0
    last_run_bash_failure: dict | None = None
    changed_since_failure: set = field(default_factory=set)
    last_learned_insight_sig: str = ""
    recent_focus_paths_updated: list = field(default_factory=list)
    last_tool_name: str = ""       # for plan tracker
    last_tool_result: str = ""     # for plan tracker
    task_complete_called: bool = False
    extra_user_msg: str = ""            # injected into dtool_msgs when set
    deadline_extended_to: float = 0.0   # DEADLINE-GRACE: new run deadline after write progress




def _cap_tool_result(result: str, max_chars: int = 8000) -> str:
    """Truncate tool results exceeding max_chars to prevent silent context-window overflow."""
    if not isinstance(result, str) or len(result) <= max_chars:
        return result
    cutoff = result.rfind("\n", 0, max_chars)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return (
        result[:cutoff]
        + "\n\n[...output truncated at "
        + str(max_chars)
        + " chars. Use read_file to retrieve full content if needed.]"
    )


# CONTEXT-COMPACTION (2026-09-04): successfully executed huge
# write/edit tool calls carry their full content (~20-30k chars) as
# function.arguments in the history. This message was never part of a
# sent prompt (it is the completion of the last round), so it is
# compacted to a small, meaningful stub right after success — the
# context stays small and the llama.cpp prefix cache is NOT invalidated.
_WRITE_ARG_COMPACT_MIN = 12000
_WRITE_ARG_COMPACT_NAMES = {
    "write_file", "write_file_append", "edit_file",
    "patch_file", "replace_lines",
}

# STUB-ECHO-GUARD (2026-09-09): the ARG-COMPACT stub below is compacted into
# the conversation history after a successful large write. Small models
# occasionally copy that stub verbatim out of their own history and "write"
# it back into the file (observed live with spark-x2.5:4b). Any write/edit
# whose arguments contain this marker is rejected before execution.
_WRITE_STUB_MARK = "content not in context anymore"


def _args_str_len(raw_args) -> int:
    if isinstance(raw_args, str):
        return len(raw_args)
    try:
        return len(json.dumps(raw_args or {}, ensure_ascii=False))
    except Exception:
        return 0


def _compact_round_write_args(messages, assistant_idx, call, dname, raw_args, dargs) -> int:
    """Compacts the arguments of a successfully executed large write call.

    Shrinks ONLY the assistant entry of this round (never sent as a prompt
    -> cache-friendly). The stub keeps path + a short reference
    (arg chars + sha1 prefix) so undo/diff/reference checks work without
    the full content. Returns the number of compacted entries.

    TODO (2026-09-06, consolidation after live observation ghosts.js/game.js):
    after the stub the model no longer sees the written content and often
    rewrites the same file COMPLETELY in later rounds (write_file churn).
    IMPLEMENTED (b) 2026-09-09: the stub carries an imperative read-first
    nudge. (a)/(c) remain available if churn persists - see also
    duo_write_guard_enabled.
    """
    if not (dname in _WRITE_ARG_COMPACT_NAMES and messages and 0 <= int(assistant_idx) < len(messages)):
        return 0
    _alen = _args_str_len(raw_args)
    if _alen < _WRITE_ARG_COMPACT_MIN:
        return 0
    _msg = messages[int(assistant_idx)]
    _tcs = _msg.get("tool_calls") or []
    if not _tcs:
        return 0
    try:
        _raw_s = raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {}, ensure_ascii=False)
        _dig = hashlib.md5(_raw_s.encode("utf-8", "ignore")).hexdigest()[:8]
    except Exception:
        _dig = "?"
    _path = str((dargs or {}).get("path", "") or "")
    _stub = json.dumps({
        "path": _path,
        "content": f"[executed {dname}: {_alen} arg chars (sha {_dig}) - "
                   "content NOT in context anymore - read_file this path before "
                   "any further write/edit to avoid full-file rewrites]",
    }, ensure_ascii=False)
    _cid = (call or {}).get("id")
    for _tc in _tcs:
        if _cid and _tc.get("id") == _cid:
            _fn = _tc.setdefault("function", {})
            if not isinstance(_fn, dict):
                _fn = {}
                _tc["function"] = _fn
            _fn["arguments"] = _stub
            return 1
    return 0


# ═══════════════════════════════════════════════════════════════════════════
#  Main entry point
# ═══════════════════════════════════════════════════════════════════════════

async def execute_tool_round(
    *,
    tool_calls: list[dict],
    dtool_msgs: list[dict],
    round_state: DuoRoundState,
    hooks: ToolExecHooks,
    trs: ToolRoundState,          # round-level mutable state (17 slots bundled)
    # ── immutable config ──
    tool_mode: str,
    duo_ws: bool,
    workspace_lock: str,
    exec_model: str,
    auto_test_before_complete: bool = False,
    exec_has_thinking: bool,
    tool_think_auto_mode: str,
    run_id_global: str,
    chat_id: str,
    subtask_index: int,
    _MAX_FOCUS_PATHS: int = 5) -> ToolExecResult:
    """Execute all tool calls for one round. Mutates dtool_msgs, round_state,
    and the passed-in mutable structures in-place. Returns structured result."""

    if trs.task_complete_blocked_count is None:
        trs.task_complete_blocked_count = [0]

    result = ToolExecResult(
        verify_mutation_serial=trs.verify_mutation_serial,
        verify_last_ok_serial=trs.verify_last_ok_serial,
        last_run_bash_failure=trs.last_run_bash_failure,
        changed_since_failure=trs.changed_since_failure,
        last_learned_insight_sig=trs.last_learned_insight_sig)
    _dname = _dresult = ""
    # LADDER-PERSIST (2026-09-09): ladder counters live in trs across rounds.
    _round_noop_hints: list[str] = []  # NO-OP hint (2026-09-07): appended as user msg at end of round
    if trs.total_tool_errors is None:
        trs.total_tool_errors = [0]
    _total_tool_errors = trs.total_tool_errors
    _ws_root = Path(workspace_lock) if workspace_lock else Path(os.environ.get("HIVEMIND_WORKSPACE", "."))
    _is_git_repo = (_ws_root / ".git").exists()

    # CONTEXT-COMPACTION: this round's assistant message (last one with tool_calls)
    # - never sent as a prompt, so safe to compact right after success.
    _assistant_idx = -1
    for _ai in range(len(dtool_msgs) - 1, -1, -1):
        if dtool_msgs[_ai].get("role") == "assistant" and dtool_msgs[_ai].get("tool_calls"):
            _assistant_idx = _ai
            break

    _pre_results: dict[int, str] = await _prefetch_readonly_tools(
        tool_calls, exec_model, workspace_lock, tool_mode, duo_ws)

    _warn_duplicate_write_targets(tool_calls, dtool_msgs)

    for _i, _dtc_call in enumerate(tool_calls):
        if time.time() >= trs.duo_deadline_at:
            result.duo_timed_out = True
            _logger.warning("[EXEC-LD-RAW] loop_detected gesetzt (tool=%s)", _dname)
            result.loop_detected = True
            _logger.warning("[EXEC-LOOP-DIAG] loop_detected via deadline (tool=%s)",
                            _dtc_call.get("function", {}).get("name", "?"))
            _remaining = [_tc.get("function", {}).get("name", "?") for _tc in tool_calls[_i + 1:]]
            if _remaining:
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"[TIMEOUT] Tool round exceeded deadline. "
                    f"These tool calls were NOT executed and must be retried: "
                    f"{', '.join(_remaining)}."
                )})
            break

        _dfn = _dtc_call.get("function", {})
        _dname = _dfn.get("name", "")
        _raw_args = _dfn.get("arguments", {})
        # ARGS-ONCE (2026-09-09): one serialized form reused by stub check,
        # compaction size and loop sig (was 4x json.dumps/str per big write).
        _raw_s = _raw_args if isinstance(_raw_args, str) else json.dumps(_raw_args or {}, ensure_ascii=False)
        _dargs = _parse_tool_args(_raw_args)
        _dresult = ""
        _args_parse_failed = (
            isinstance(_raw_args, str)
            and _raw_args.strip() not in ("", "{}", "null")
            and not _dargs
        )

        if _args_parse_failed:
            dtool_msgs.append({
                "role": "tool",
                "content": _tool_error_response(
                    "INVALID_JSON",
                    "Tool arguments are not valid JSON. Return a valid JSON object for this tool call.",
                    tool=_dname, mode=tool_mode ),
                "tool_call_id": _dtc_call.get("id", _dname),
                "name": _dname,
            })
            continue

        _focus_path = _track_focus_path(_dargs, _dname, trs.tool_ctx_lru, trs.recent_focus_paths, _MAX_FOCUS_PATHS)

        if _dname in _WRITE_ARG_COMPACT_NAMES and _WRITE_STUB_MARK in _raw_s.lower():
            _logger.warning("[STUB-ECHO] %s blocked: compaction stub found in write args (path=%s)",
                            _dname, str((_dargs or {}).get("path", "?")) or "?")
            dtool_msgs.append({
                "role": "tool",
                "content": _tool_error_response(
                    "STUB_ECHO_BLOCKED",
                    "Blocked: these arguments contain the internal compaction stub, not real file content. "
                    "Call read_file on the path to load the current content, then write the real content.",
                    tool=_dname, mode=tool_mode ),
                "tool_call_id": _dtc_call.get("id", _dname),
                "name": _dname,
            })
            continue

        if not _dargs and _dname in ("patch_file", "edit_file", "write_file", "write_file_append", "read_file"):
            dtool_msgs.append({
                "role": "tool",
                "content": _tool_error_response(
                    "INVALID_ARGUMENT",
                    "No arguments received. Call read_file first and retry with complete arguments.",
                    tool=_dname, mode=tool_mode ),
                "tool_call_id": _dtc_call.get("id", _dname),
                "name": _dname,
            })
            continue

        await hooks.emit(_make_tool_call_event(_dname, _dargs))

        _dargs_with_model = {**_dargs, "__model__": exec_model}

        # ── ask_user ──
        # ASK-USER-TOOL-GUARD (2026-09-02): only announce/pause for a genuine
        # ask_user tool call. Previously S6 ran for EVERY tool in the round when
        # the gate was "open", so write_file/read_file/edit_file rounds emitted a
        # spurious "Your input is needed" while the run never paused (the pausing
        # handler in tools/runner.py is only reached via the ask_user dispatch).
        if _dname == "ask_user":
            await _handle_ask_user(_dname, _dargs, hooks, exec_model, trs.cached_coder_port, run_id_global)
        _dresult = await _execute_one_tool(
            _dname, _dargs, _dargs_with_model, _i, _pre_results,
            trs.duo_seen_web_queries, workspace_lock, tool_mode, duo_ws)
        await hooks.emit(_make_tool_result_event(_dname, _dresult))
        if _dname == "ask_user":
            # ASK-USER-GATE-FIX (2026-09-02): agent_resumed only when the run
            # actually paused (gate == "open"). In throttled/autonomous mode
            # tools/runner.py answers without pausing — no resume event.
            try:
                from tools.runner import _ask_user_gate as _ask_gate_cv2
                _ask_paused = _ask_gate_cv2.get("open") == "open"
            except Exception:
                _ask_paused = True
            if _ask_paused:
                await hooks.emit({"type": "agent_resumed"})

        # ── Until-Finished stuck detection + user abort ──
        if hooks.on_tool_result:
            _should_break = await hooks.on_tool_result(_dname, _dargs, _dresult)
            if _should_break:
                _logger.warning("[EXEC-LOOP-DIAG] loop_detected via on_tool_result hook (tool=%s)", _dname)
                result.loop_detected = True
                break

        if hooks.is_aborted(chat_id):
            _logger.warning("[EXEC-LOOP-DIAG] loop_detected via is_aborted chat=%s (tool=%s)", chat_id, _dname)
            result.loop_detected = True
            break

        # ── Reactive tool-thinking ──
        await _maybe_activate_reactive_think(_dname, _dresult, round_state, dtool_msgs,
                                              hooks, tool_think_auto_mode, exec_has_thinking)
        # ── run_bash: fail→fix→pass insight ──
        await _run_bash_fail_fix_pass_insight(_dname, _dargs, _dresult, result, hooks,
                                               workspace_lock, subtask_index)
        # ── Auto-retry: patch_file fallback hint ──
        _dresult = _patch_file_fallback_hint(_dname, _dargs, _dresult, trs.attempts_per_file)
        # ── READ_REQUIRED loop-break: 3 consecutive blocks → hard cap ──
        _dresult = _read_required_and_python_hints(_dname, _dargs, _dresult, trs.tool_error_retries)
        # ── Too-large content handling ──
        _is_too_large = (
            _tool_error_has_code(_dresult, "EDIT_FILE_CONTENT_TOO_LARGE", "edit_file")
            or _tool_error_has_code(_dresult, "WRITE_FILE_CONTENT_TOO_LARGE", "write_file")
            or _tool_error_has_code(_dresult, "WRITE_FILE_APPEND_CHUNK_TOO_LARGE", "write_file_append")
            or "[edit_file error: content too large" in _dresult
            or "[write_file error: content too large" in _dresult
            or "[write_file_append error: chunk too large" in _dresult
        )
        _dresult = _cap_tool_result(_dresult)
        if _is_too_large:
            await _handle_too_large(_dname, _dargs, _dresult, _dtc_call, dtool_msgs,
                                    trs.last_too_large_path, round_state, hooks)
        else:
            _dresult, _hint_matched = _inject_tool_error_hints(
                _dname, _dargs, _dresult, _dtc_call, dtool_msgs, trs.attempts_per_file, trs.tool_error_retries,
                workspace_lock=workspace_lock)
            if _hint_matched:
                pass
            elif _dname == "task_complete":
                # TC-DE-NAG (2026-09-10): a task_complete re-call WITHOUT any
                # other tool in between means the model ignored the previous
                # feedback — accept instead of nagging again (each blocked call
                # costs a full LLM round + auto-test probe).
                trs.tc_consecutive += 1
                if trs.tc_consecutive >= 2:
                    _logger.warning("[TC-DE-NAG] consecutive task_complete accepted (call #%d)", trs.tc_consecutive)
                    result.task_complete_called = True
                    break
                dtool_msgs.append({"role": "tool", "content": _dresult,
                                    "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
                _mutations_made = result.verify_mutation_serial > 0
                _tc_blocked = "blocked" in str(_dresult).lower() and "build_status" in str(_dresult).lower()

                # ── AUTO-TEST vor task_complete (2026-08-12, B) ──────────────────
                _auto_test_skips_gate = False
                # SMOKE-NUDGE flag persists in trs (was function-local: reset every round)
                if auto_test_before_complete and _mutations_made:
                    _last_write_idx_at = -1
                    _last_test_idx_at = -1
                    for _at_idx, _at_m in enumerate(dtool_msgs):
                        if _at_m.get("role") != "tool":
                            continue
                        _at_name = _at_m.get("name", "")
                        _at_content = str(_at_m.get("content") or "")
                        if _at_name in ("write_file", "edit_file", "patch_file", "write_file_append", "replace_lines"):
                            _last_write_idx_at = _at_idx
                        elif _at_name == "run_tests" and not _at_content.startswith("[AUTO-TEST]"):
                            _last_test_idx_at = _at_idx
                        elif _at_name == "run_bash":
                            if ("[TEST-RESULT]" in _at_content
                                    or re.search(r"\b(pytest|npm test|vitest|jest|cargo test|go test|mvn test|dotnet test)\b",
                                                 _at_content, re.IGNORECASE)):
                                _last_test_idx_at = _at_idx
                    if _last_test_idx_at <= _last_write_idx_at:
                        _logger.info("[AUTO-TEST] No test run since last edit - running run_tests")
                        try:
                            _at_res = await _run_inline_tool(
                                "run_tests", {"timeout": 90, "__model__": exec_model},
                                workspace_lock=workspace_lock,
                                tool_mode=tool_mode, include_websearch=duo_ws,
                            )
                        except Exception as _ate:
                            _at_res = _tool_error_response(
                                "RUN_TESTS_EXEC_ERROR",
                                f"{type(_ate).__name__}: {str(_ate)[:150]}",
                                tool="run_tests" )
                        dtool_msgs.append({"role": "tool", "content": "[AUTO-TEST]" + _at_res,
                                            "tool_call_id": _dtc_call.get("id", _dname), "name": "run_tests"})
                        try:
                            await hooks.emit({"type": "token",
                                "content": f"\n🧪 Auto-Test vor task_complete:\n{_at_res[:500]}\n"})
                        except Exception:
                            pass
                        if "[TEST-RESULT] ✅" in _at_res:
                            result.task_complete_called = True
                            _logger.info("[AUTO-TEST] Tests green - task_complete allowed through")
                            break
                        if _at_res.startswith("[TEST-RESULT] ⚠️"):
                            # SMOKE-NUDGE (2026-09-10): no test framework found —
                            # nudge the coder ONCE to write a minimal smoke test
                            # and run it, instead of silently passing.
                            if not trs.at_nosuite_nudged:
                                trs.at_nosuite_nudged = True
                                _logger.info("[AUTO-TEST] No test suite — nudging coder to write a minimal smoke test")
                                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                                    "[NO TEST SUITE] No test framework was detected. Before completing: "
                                    "write a minimal smoke test that loads/executes the files you created and "
                                    "fails on errors (e.g. tests/smoke_test.py, or a quick headless check for "
                                    "HTML/JS projects). Run it via run_bash and ensure exit code 0, then call "
                                    "task_complete again."
                                )})
                                _auto_test_skips_gate = True
                                continue
                            _logger.info("[AUTO-TEST] No tests in the project - task_complete allowed through (smoke test nudge already given)")
                            result.task_complete_called = True
                            break
                        _gate = _task_complete_ladder(
                            trs, dtool_msgs, result, True,
                            "[AUTO-TEST BLOCKED] task_complete rejected: the test suite is NOT green "
                            "(see TEST-RESULT above). Fix the failures, then run_tests again — "
                            "task_complete is only allowed once tests pass.",
                            "[task_complete ALLOWED] Tests failed 3x — accepting task_complete "
                            "with the failing test status so the run can end.")
                        _auto_test_skips_gate = True
                        if _gate == "block":
                            continue
                        break

                # ── Ordered bash check: last run_bash must be AFTER last edit AND successful ──
                # (Bei aktivem Auto-Test: BLOCKED → continue, ALLOWED → break oben —
                _last_edit_idx  = -1
                _last_bash_idx   = -1
                _last_bash_failed = False
                _last_bash_verified = False
                _last_append_idx = -1
                _last_tool_name = ""
                for _idx, _m in enumerate(dtool_msgs):
                    if _m.get("role") != "tool":
                        continue
                    _tname = _m.get("name", "")
                    _last_tool_name = _tname
                    if _tname in ("write_file", "edit_file", "patch_file", "write_file_append", "replace_lines"):
                        _last_edit_idx = _idx
                    if _tname == "write_file_append":
                        _last_append_idx = _idx
                    elif _tname == "run_bash":
                        _content = str(_m.get("content") or "")
                        _last_bash_idx = _idx
                        _ec_match = re.search(r'\[exit code:\s*(\d+)\]', _content)
                        if _ec_match and int(_ec_match.group(1)) != 0:
                            _last_bash_failed = True
                            _last_bash_verified = False
                        else:
                            _last_bash_failed = False
                            _last_bash_verified = bool(re.search(
                                r"(?i)(\bpassed\b|\bsuccess\b|successful|\u2713|\u2705|TEST-RESULT"
                                r"|\bbuilt\b|compiled|0 failed|0 errors|no (errors|vulnerabilities))",
                                _content,
                            ))
                    elif _tname == "run_tests":
                        _content = str(_m.get("content") or "")
                        _last_bash_idx = _idx
                        _last_bash_failed = ("[TEST-RESULT] ✅" not in _content
                                             and "[TEST-RESULT] ⚠️" not in _content)
                        _last_bash_verified = not _last_bash_failed

                def _chunk_incomplete_p():


                    if _last_append_idx >= _last_edit_idx and _last_append_idx != -1:
                        _task_complete_ladder(
                            trs, dtool_msgs, result, bool(_tc_blocked),
                            "[CHUNK INCOMPLETE] The last file operation was "
                            "write_file_append — a chunk sequence may be "
                            "unfinished. Write your final chunk or confirm "
                            "completion with a different tool call before "
                            "calling task_complete.",
                            _TC_ALLOWED_MSG if _tc_blocked else None)
                        return True
                    return False

                if not _mutations_made:
                    # Signal wie "passed"/"success"/"TEST-RESULT"). Triviale exit-0-Kommandos
                    if _last_bash_idx != -1 and not _last_bash_failed and _last_bash_verified:
                        result.task_complete_called = True
                    else:
                        _gate = _task_complete_ladder(
                            trs, dtool_msgs, result, bool(_tc_blocked),
                            "[task_complete BLOCKED] No file edits were made this run. "
                            "You must make at least one edit (write_file/edit_file) "
                            "or verify something with run_bash before completing. Continue.",
                            _TC_ALLOWED_MSG if _tc_blocked else None)
                        if _gate == "allow_break":
                            break

                elif _last_bash_idx == -1:
                    if _chunk_incomplete_p():
                        pass  # blocked with message; loop continues
                    else:
                        result.task_complete_called = True
                    
                        break

                elif _last_bash_idx <= _last_edit_idx:
                    _gate = _task_complete_ladder(
                        trs, dtool_msgs, result, bool(_tc_blocked),
                        "[VERIFY REQUIRED] Code was changed since the last "
                        "run_bash — changes are unverified. Call run_tests now; "
                        "if it reports no suite, run the project's documented check "
                        "via run_bash (e.g. python selftest.py) and ensure exit code 0. "
                        "Then call task_complete.",
                        _TC_ALLOWED_MSG if _tc_blocked else None)
                    if _gate == "allow_break":
                        break
                    if _gate == "allow":
                        _logger.warning("[EXEC-LD-RAW] loop_detected gesetzt (tool=%s)", _dname)

                elif _last_bash_failed:
                    _gate = _task_complete_ladder(
                        trs, dtool_msgs, result, bool(_tc_blocked),
                        "[VERIFY FAILED] The last run_bash exited with a "
                        "non-zero exit code. Fix the error and run your tests "
                        "again before calling task_complete.",
                        _TC_ALLOWED_MSG if _tc_blocked else None)
                    if _gate == "allow_break":
                        break
                    if _gate == "allow":
                        _logger.warning("[EXEC-LD-RAW] loop_detected gesetzt (tool=%s)", _dname)

                else:
                    _logger.warning("[EXEC-LD-RAW] loop_detected gesetzt (tool=%s)", _dname)
                    if _chunk_incomplete_p():
                        pass  # blocked with message; loop continues
                    else:
                        result.task_complete_called = True
                        break
            else:
                _unknown_error_hint(_dname, _dresult, _dtc_call, dtool_msgs, trs.tool_error_retries)

        _is_verify_feedback = (
            _dname in ("run_bash", "run_python")
            and "NONZERO" in str(_dresult or "")
        )
        if _tool_call_failed(_dresult, _dname) and not _is_verify_feedback:
            _total_tool_errors[0] += 1
            if _total_tool_errors[0] >= 6:
                if not _recovery_saturated(dtool_msgs):
                    dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                        "[SYSTEM] 6 tool calls have failed across different "
                        "error types. The current approach is fundamentally "
                        "not working. Call task_complete with a summary of "
                        "what was accomplished and what remains."
                    )})
                _logger.warning("[EXEC-LD-RAW] loop_detected gesetzt (tool=%s)", _dname)
                result.loop_detected = True
                break

        # Parse errors: decrement on successful write/patch
        _note_successful_write(_dname, _dresult, result, round_state, _total_tool_errors)
        # NO-OP HINT (2026-09-07): compression-proof run-state streak for
        # ineffective edits — collected; appended to the message end only after
        # the complete round (template-safe, like [CTX-HORIZON]).
        try:
            _hint_noop = _track_edit_noop(
                _dname, _dresult,
                str(_focus_path or (_dargs or {}).get("path", "") or ""),
                round_state,
            )
            if _hint_noop:
                _round_noop_hints.append(_hint_noop)
        except Exception:
            pass

        # ── File-change tracking ──
        await _track_file_changes(_dname, _dargs, _dresult, result, trs.file_changes,
                                   dtool_msgs, hooks, _is_git_repo)
        # ── LRU-A: stale-read invalidation ──
        # After a successful edit/write/patch/append, any read_file output of the
        # same path already in context is stale (the file on disk changed). Evict
        # it immediately so the model neither wastes context nor trusts outdated
        # content for follow-up edits.
        if (
            _dname in ("edit_file", "write_file", "patch_file", "write_file_append", "replace_lines")
            and _focus_path
            and not _tool_call_failed(_dresult, _dname)
        ):
            try:
                from context.compression import evict_stale_reads_for_path as _evict_stale
                _evicted_stale = _evict_stale(
                    messages=dtool_msgs,
                    lru=trs.tool_ctx_lru,
                    path=_focus_path,
                    cache_horizon=trs.cache_horizon,
                    superseded=trs.superseded_paths,
                )
                if _evicted_stale:
                    _logger.info(
                        "[LRU-STALE] %s invalidated %d stale read_file output(s) of %s",
                        _dname, _evicted_stale, _focus_path)
            except Exception as _ev_stale_err:
                _logger.debug("[LRU-STALE] invalidation failed: %s", _ev_stale_err)
            # DEADLINE-GRACE (2026-09-09): a successful mutation is progress,
            # not a stuck loop. Large completions on slow backends easily
            # outlive the run deadline mid-task (live: a 4B coder was killed
            # before its legitimate recovery read_file after 5 big write
            # rounds). Push the deadline forward instead of aborting
            # productive work; the hard tool-round cap still bounds runaways.
            _grace_at = time.time() + 300.0
            if _grace_at > trs.duo_deadline_at:
                trs.duo_deadline_at = _grace_at
                result.deadline_extended_to = _grace_at
                _logger.info(
                    "[DEADLINE-GRACE] %s succeeded - run deadline extended by +300s",
                    _dname)
            # CONTEXT-COMPACTION: compact this round's huge args after success
            # (never sent as a prompt -> cache-friendly, saves context).
            _saved_chars = len(_raw_s)
            if _compact_round_write_args(dtool_msgs, _assistant_idx, _dtc_call, _dname, _raw_args, _dargs):
                _logger.info(
                    "[ARG-COMPACT] %s args compacted to stub after success "
                    "(focus=%s, saved_chars=%d)",
                    _dname, _focus_path, _saved_chars)
        # ── Context LRU registration ──
        _register_context_lru(dtool_msgs, trs.tool_ctx_lru, _focus_path, _dname, _dresult,
                              cache_horizon=trs.cache_horizon, superseded=trs.superseded_paths)
        # ── Read-file ladder tracker (persistent across rounds) ──
        _update_read_ladder(trs, _dname, _args_parse_failed, _focus_path or "")
        if _dname != "task_complete":
            trs.tc_consecutive = 0
        _consecutive_reads = trs.consecutive_reads
        _read_ladder_fired = trs.read_ladder_fired

        # ── Loop detection ──
        _args_str = _raw_s
        _new_sig = _dname + "|" + hashlib.md5(_args_str.encode("utf-8", errors="replace")).hexdigest()
        if not str(_dresult or "").startswith("[SKIP:"):
            trs.call_sigs.append(_new_sig)
            trs.call_sigs[:] = trs.call_sigs[-6:]
        _last2_identical = (len(trs.call_sigs) >= 2 and len(set(trs.call_sigs[-2:])) == 1)
        _last3_identical = (len(trs.call_sigs) >= 3 and len(set(trs.call_sigs[-3:])) == 1)
        _period2_loop = (len(trs.call_sigs) >= 4 and trs.call_sigs[-4:-2] == trs.call_sigs[-2:])
        _period3_loop = (len(trs.call_sigs) >= 6 and trs.call_sigs[-6:-3] == trs.call_sigs[-3:])

        if _last3_identical or _period2_loop or _period3_loop:
            _loop_label = (
                "3x identical" if _last3_identical
                else ("ABAB pattern" if _period2_loop else "ABCABC pattern")
            )
            if _dname == "run_bash":
                # BASH-LOOP-REINJECT (2026-09-02): repeated run_bash calls in a
                # loop must NOT abort the run. Instead re-inject the full tool
                # list so the model can pick a different tool, then let the round
                # continue (round budget still bounds the loop).
                _menu = _render_tool_menu(tool_mode, duo_ws)
                await hooks.emit({"type": "token", "content": f"\n[Tool loop: {_loop_label} — re-injecting all tools]\n"})
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"[LOOP DETECTED] run_bash was called repeatedly with the same or "
                    f"equivalent command ({_loop_label}). The result will not change, so "
                    f"DO NOT call run_bash again with the same approach.\n"
                    f"STOP and pick a DIFFERENT tool. Your full toolset:\n{_menu}\n"
                    f"Re-read the actual files (read_file), inspect the failing test file "
                    f"directly, or call task_complete(status='blocked', reason='...') if you "
                    f"are genuinely stuck. Choose a non-run_bash action now."
                )})
                trs.call_sigs.clear()
                break
            await hooks.emit({"type": "token", "content": f"\n[Tool loop: {_loop_label} — aborted]\n"})
            dtool_msgs.append({"role": "tool", "content": "[loop-detection: aborted]",
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
            _logger.warning("[EXEC-LD-RAW] loop_detected gesetzt (tool=%s)", _dname)
            result.loop_detected = True
            break

        # ── Soft 2x check: read-only tools (hint only, no break) ──
        elif _last2_identical and _dname in ("search_code", "find_files", "git_status", "list_dir"):
            if not _recovery_saturated(dtool_msgs):
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"Same {_dname} call with identical args called twice — "
                    f"the result will not change. Skip this call and try a "
                    f"different pattern, path, or tool instead."
                )})

        # ── Read-file ladder: 3+ consecutive reads with no write/edit ──
        elif _consecutive_reads >= 3 and not _read_ladder_fired and any(
            isinstance(_m, dict) and _m.get("role") == "tool" and _m.get("name") in (
                "edit_file", "write_file", "patch_file", "write_file_append",
                "replace_lines", "run_bash", "run_python")
            for _m in dtool_msgs
        ):
            if not _recovery_saturated(dtool_msgs):
                _logger.info("[READ-LADDER] fired consecutive=%d recovery_saturated=False", _consecutive_reads)
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"[READ LADDER] {_consecutive_reads} consecutive read_file calls without any write/edit. "
                    f"You are exploring but not implementing. Pick the MOST RELEVANT file you've read and "
                    f"call write_file or edit_file on it NOW. Do NOT read any more files until you've "
                    f"made a change."
                )})
            else:
                _logger.info("[READ-LADDER] skipped (recovery_saturated=True) consecutive=%d", _consecutive_reads)
            await hooks.emit({"type": "token", "content": f"\n[Read-Ladder: {_consecutive_reads}x reads ohne Write — Hint injiziert]\n"})
            trs.read_ladder_fired = True
            trs.consecutive_reads = 0

    # NO-OP HINT (2026-09-07): append only after all tool results of this
    # round, so the assistant(tool_calls) -> tool-result ordering stays
    # intact.
    if _round_noop_hints:
        for _nh in _round_noop_hints:
            dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX + _nh)})

    # CACHE-HORIZON (2026-09-04): superseded read_file outputs live in the
    # already-sent prefix and were NOT replaced in-place. One-time tail note
    # so the model does not trust the older (stale) content.
    if getattr(trs, "superseded_paths", None):
        _sup_clean = []
        for _sp in trs.superseded_paths:
            if _sp and _sp not in _sup_clean:
                _sup_clean.append(_sp)
        if _sup_clean:
            dtool_msgs.append({"role": "user", "content": (
                _SYS_PREFIX +
                "[CTX-HORIZON] Earlier read_file output(s) for the following path(s) "
                "are superseded by newer changes and must be ignored: "
                + ", ".join(_sup_clean[:8])
                + ". The current file state (from your most recent write/edit or the "
                "newest read_file) is authoritative. Do NOT edit based on the older copy."
            )})

    # Track last tool call for plan tracker
    result.last_tool_name = _dname
    result.last_tool_result = _dresult
    result.file_changes = trs.file_changes
    result.recent_focus_paths_updated = trs.recent_focus_paths
    return result