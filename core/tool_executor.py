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
    _SYS_PREFIX, _RECOVERY_PHRASES, _recovery_saturated,
    _prefetch_readonly_tools, _warn_duplicate_write_targets, _track_focus_path,
    _note_successful_write, _update_read_ladder,
    _handle_too_large, _inject_tool_error_hints,
    _handle_ask_user, _execute_one_tool, _maybe_activate_reactive_think,
    _run_bash_fail_fix_pass_insight, _patch_file_fallback_hint,
    _read_required_and_python_hints, _unknown_error_hint,
    _track_file_changes, _register_context_lru,
)

_logger = logging.getLogger("tool_executor")


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


_RECOVERY_PHRASES = (
    "Do NOT retry", "Do NOT call", "Use write_file",
    "Call write_file NOW", "[SYSTEM]", "[REPEATED",
    "[READ LADDER]", "has occurred", "not working",
    "You MUST split", "Switch strategy", "Do NOT retry",
    "CALL A TOOL", "Your approach", "Switch to",
    "Do NOT repeat", "Try a different", "same result.",
    "[CTX:", "CTX CRITICAL", "[AUTOMATED TOOL SYSTEM]",
)

def _recovery_saturated(msgs: list) -> bool:
    """True if last 6 messages already carry 3+ recovery hints."""
    recent = msgs[-6:] if len(msgs) >= 6 else msgs
    count = sum(
        1 for m in recent
        if m.get("role") in ("user", "system")
        and any(phrase in (m.get("content") or "")
                for phrase in _RECOVERY_PHRASES)
    )
    return count >= 3


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
    _build_fix_insight = None  # lazy import
    _dname = _dresult = ""
    _consecutive_reads = 0
    if trs.total_tool_errors is None:
        trs.total_tool_errors = [0]
    _total_tool_errors = trs.total_tool_errors
    _ws_root = Path(workspace_lock) if workspace_lock else Path(os.environ.get("HIVEMIND_WORKSPACE", "."))
    _is_git_repo = (_ws_root / ".git").exists()

    _pre_results: dict[int, str] = await _prefetch_readonly_tools(
        tool_calls, exec_model, workspace_lock, tool_mode, duo_ws)

    _warn_duplicate_write_targets(tool_calls, dtool_msgs)

    for _i, _dtc_call in enumerate(tool_calls):
        if time.time() >= trs.duo_deadline_at:
            result.duo_timed_out = True
            _logger.warning("[EXEC-LD-RAW] loop_detected gesetzt (tool=%s)", _dname)
            result.loop_detected = True
            _logger.warning("[EXEC-LOOP-DIAG] loop_detected via deadline (tool=%s)", _dfn if "_dfn" in dir() else "?")
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
        _dresult = _cap_tool_result(_dresult)
        if _is_too_large:
            await _handle_too_large(_dname, _dargs, _dresult, _dtc_call, dtool_msgs,
                                    trs.last_too_large_path, round_state, hooks)
        else:
            _dresult, _hint_matched = _inject_tool_error_hints(
                _dname, _dargs, _dresult, _dtc_call, dtool_msgs, trs.attempts_per_file, trs.tool_error_retries)
            if _hint_matched:
                pass
            elif _dname == "task_complete":
                dtool_msgs.append({"role": "tool", "content": _dresult,
                                    "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
                _mutations_made = result.verify_mutation_serial > 0
                _tc_blocked = "blocked" in str(_dresult).lower() and "build_status" in str(_dresult).lower()

                # ── AUTO-TEST vor task_complete (2026-08-12, B) ──────────────────
                _auto_test_skips_gate = False
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
                            _logger.info("[AUTO-TEST] No tests in the project - task_complete allowed through (manual verification)")
                            result.task_complete_called = True
                            break
                        trs.task_complete_blocked_count[0] += 1
                        if trs.task_complete_blocked_count[0] < 3:
                            dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                                "[AUTO-TEST BLOCKED] task_complete rejected: the test suite is NOT green "
                                "(see TEST-RESULT above). Fix the failures, then run_tests again — "
                                "task_complete is only allowed once tests pass."
                            )})
                            _auto_test_skips_gate = True
                            continue
                        else:
                            result.task_complete_called = True
                            dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                                "[task_complete ALLOWED] Tests failed 3x — accepting task_complete "
                                "with the failing test status so the run can end."
                            )})
                            _auto_test_skips_gate = True
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
                        trs.task_complete_blocked_count[0] += 1
                        if trs.task_complete_blocked_count[0] >= 3 and _tc_blocked:
                            dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                                "[task_complete ALLOWED] Accepted with status=blocked "
                                "after repeated attempts — proceeding."
                            )})
                            result.task_complete_called = True
                        
                            return True
                        elif trs.task_complete_blocked_count[0] >= 3:
                        
                            result.task_complete_called = True
                            return True
                        else:
                            dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                                "[CHUNK INCOMPLETE] The last file operation was "
                                "write_file_append — a chunk sequence may be "
                                "unfinished. Write your final chunk or confirm "
                                "completion with a different tool call before "
                                "calling task_complete."
                            )})
                            return True
                    return False

                if not _mutations_made:
                    # Signal wie "passed"/"success"/"TEST-RESULT"). Triviale exit-0-Kommandos
                    if _last_bash_idx != -1 and not _last_bash_failed and _last_bash_verified:
                        result.task_complete_called = True
                    else:
                        trs.task_complete_blocked_count[0] += 1
                        if trs.task_complete_blocked_count[0] >= 3 and _tc_blocked:
                            dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                                "[task_complete ALLOWED] Accepted with status=blocked "
                                "after repeated attempts — proceeding."
                            )})
                            result.task_complete_called = True
                            break
                        elif trs.task_complete_blocked_count[0] >= 3:
                            result.task_complete_called = True
                        else:
                            dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                                "[task_complete BLOCKED] No file edits were made this run. "
                                "You must make at least one edit (write_file/edit_file) "
                                "or verify something with run_bash before completing. Continue."
                            )})

                elif _last_bash_idx == -1:
                    if _chunk_incomplete_p():
                        pass  # blocked with message; loop continues
                    else:
                        result.task_complete_called = True
                    
                        break

                elif _last_bash_idx <= _last_edit_idx:
                    trs.task_complete_blocked_count[0] += 1
                    if trs.task_complete_blocked_count[0] >= 3 and _tc_blocked:
                        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                            "[task_complete ALLOWED] Accepted with status=blocked "
                            "after repeated attempts — proceeding."
                        )})
                        result.task_complete_called = True
                    
                        break
                    elif trs.task_complete_blocked_count[0] >= 3:
                        _logger.warning("[EXEC-LD-RAW] loop_detected gesetzt (tool=%s)", _dname)
                        result.task_complete_called = True
                    else:
                        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                            "[VERIFY REQUIRED] Code was changed since the last "
                            "run_bash — changes are unverified. Call run_tests now; "
                            "if it reports no suite, run the project's documented check "
                            "via run_bash (e.g. python selftest.py) and ensure exit code 0. "
                            "Then call task_complete."
                        )})

                elif _last_bash_failed:
                    trs.task_complete_blocked_count[0] += 1
                    if trs.task_complete_blocked_count[0] >= 3 and _tc_blocked:
                        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                            "[task_complete ALLOWED] Accepted with status=blocked "
                            "after repeated attempts — proceeding."
                        )})
                        result.task_complete_called = True
                    
                        break
                    elif trs.task_complete_blocked_count[0] >= 3:
                        _logger.warning("[EXEC-LD-RAW] loop_detected gesetzt (tool=%s)", _dname)
                        result.task_complete_called = True
                    else:
                        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                            "[VERIFY FAILED] The last run_bash exited with a "
                            "non-zero exit code. Fix the error and run your tests "
                            "again before calling task_complete."
                        )})

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
                )
                if _evicted_stale:
                    _logger.info(
                        "[LRU-STALE] %s invalidated %d stale read_file output(s) of %s",
                        _dname, _evicted_stale, _focus_path)
            except Exception as _ev_stale_err:
                _logger.debug("[LRU-STALE] invalidation failed: %s", _ev_stale_err)
        # ── Context LRU registration ──
        _register_context_lru(dtool_msgs, trs.tool_ctx_lru, _focus_path, _dname, _dresult)
        # ── Read-file ladder tracker ──
        _consecutive_reads = _update_read_ladder(_dname, _args_parse_failed, _consecutive_reads)

        # ── Loop detection ──
        _args_str = str(_dfn.get("arguments", ""))
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
        elif _consecutive_reads >= 3 and any(
            isinstance(_m, dict) and _m.get("role") == "tool" and _m.get("name") in (
                "edit_file", "write_file", "patch_file", "write_file_append",
                "replace_lines", "run_bash", "run_python")
            for _m in dtool_msgs
        ):
            if not _recovery_saturated(dtool_msgs):
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"[READ LADDER] {_consecutive_reads} consecutive read_file calls without any write/edit. "
                    f"You are exploring but not implementing. Pick the MOST RELEVANT file you've read and "
                    f"call write_file or edit_file on it NOW. Do NOT read any more files until you've "
                    f"made a change."
                )})
            await hooks.emit({"type": "token", "content": f"\n[Read-Ladder: {_consecutive_reads}x reads ohne Write — Hint injiziert]\n"})
            _consecutive_reads = 0

    # Track last tool call for plan tracker
    result.last_tool_name = _dname
    result.last_tool_result = _dresult
    result.file_changes = trs.file_changes
    result.recent_focus_paths_updated = trs.recent_focus_paths
    return result