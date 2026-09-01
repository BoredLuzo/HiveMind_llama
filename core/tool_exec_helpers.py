# -*- coding: utf-8 -*-
"""Pure tool-exec helpers (extracted from core/tool_executor.py).

Each function is a mechanical copy of a section from
``execute_tool_round`` — no behavior delta. Deliberately does NOT import
core.tool_executor (cycle avoidance); result objects are mutated via duck
typing.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import re
import re
import os
from pathlib import Path

from tools.runner import _run_inline_tool
from utils.tool import parse_tool_args as _parse_tool_args, run_bash_failed as _run_bash_failed, run_bash_failed as _run_bash_failed
from tools.errors import (
    tool_call_failed as _tool_call_failed,
    tool_error_has_code as _tool_error_has_code,
    tool_error_response as _tool_error_response,
)

_SYS_PREFIX = "[AUTOMATED TOOL SYSTEM] "


async def _prefetch_readonly_tools(tool_calls, exec_model, workspace_lock, tool_mode, duo_ws) -> dict:
    """Parallel-prefetch read-only tools (S0 part of execute_tool_round)."""
    _PARALLEL_SAFE = {"read_file", "search_code", "find_files", "list_dir",
                       "get_signatures", "find_references", "git_status", "web_fetch"}
    # web_search excluded: modifies duo_seen_web_queries (mutable set) — not parallel-safe
    _all_read_only = tool_calls and all(
        tc.get("function", {}).get("name", "") in _PARALLEL_SAFE
        for tc in tool_calls
    )
    _pre_results: dict[int, str] = {}
    if _all_read_only:
        async def _execute_one(_tc):
            _fn = _tc.get("function", {})
            _name = _fn.get("name", "")
            _args = _parse_tool_args(_fn.get("arguments", {}))
            try:
                return await _run_inline_tool(
                    _name, {**_args, "__model__": exec_model},
                    workspace_lock=workspace_lock,
                    tool_mode=tool_mode, include_websearch=duo_ws)
            except Exception as _te:
                return _tool_error_response(
                    "TOOL_EXEC_CRASH",
                    f"{type(_te).__name__}: {str(_te)[:200]}",
                    tool=_name, mode=tool_mode)
        _tasks = [_execute_one(tc) for tc in tool_calls]
        _gathered = await asyncio.gather(*_tasks, return_exceptions=True)
        for _i, _g in enumerate(_gathered):
            if isinstance(_g, BaseException):
                _pre_results[_i] = _tool_error_response(
                    "TOOL_EXEC_CRASH",
                    f"{type(_g).__name__}: {str(_g)[:200]}",
                    tool=tool_calls[_i].get("function", {}).get("name", ""),
                    mode=tool_mode)
            else:
                _pre_results[_i] = _g
    return _pre_results


def _warn_duplicate_write_targets(tool_calls, dtool_msgs) -> None:
    """Scan for duplicate write targets in one round (U1)."""
    _write_tools = {"write_file", "edit_file", "patch_file",
                    "replace_lines", "write_file_append"}
    _seen_paths: dict[str, str] = {}
    for _stc in tool_calls:
        _sname = _stc.get("function", {}).get("name", "")
        if _sname in _write_tools:
            try:
                _sargs_raw = _stc.get("function", {}).get("arguments", "{}")
                _sargs = json.loads(_sargs_raw) if isinstance(_sargs_raw, str) else {}
                _spath = _sargs.get("path", "") if isinstance(_sargs, dict) else ""
            except Exception:
                _spath = ""
            if _spath and _spath in _seen_paths:
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"[WARNING] Two writes to '{_spath}' in one round "
                    f"({_seen_paths[_spath]} then {_sname}). "
                    f"Second write will overwrite first. "
                    f"Split into separate rounds if unintentional."
                )})
            elif _spath:
                _seen_paths[_spath] = _sname


def _track_focus_path(dargs, dname, tool_ctx_lru, recent_focus_paths, max_focus_paths: int) -> str:
    """Focus-Pfad normalisieren + LRU-Decay + Ring-Puffer (S4)."""
    _focus_path = str(dargs.get("path", "") or dargs.get("file", "")).strip()
    tool_ctx_lru.decay(_focus_path)

    if _focus_path and dname in ("patch_file", "edit_file", "write_file", "read_file"):
        _fp_norm = _focus_path.replace("\\", "/").strip()
        if _fp_norm not in recent_focus_paths:
            recent_focus_paths.insert(0, _fp_norm)
            if len(recent_focus_paths) > max_focus_paths:
                recent_focus_paths.pop()
    return _focus_path


def _note_successful_write(dname, dresult, result, round_state, total_tool_errors) -> None:
    """Decrement parse errors on a successful write/patch (U5)."""
    if dname in ("patch_file", "edit_file", "write_file", "write_file_append") and not _tool_call_failed(dresult, dname):
        result.verify_mutation_serial += 1
        round_state.parse_errors = max(0, round_state.parse_errors - 1)
        total_tool_errors[0] = 0


def _update_read_ladder(dname, args_parse_failed, consecutive_reads: int) -> int:
    """Read-file ladder tracker (S17) — returns a new counter."""
    if dname == "read_file" and not args_parse_failed:
        return consecutive_reads + 1
    if dname in ("edit_file", "write_file", "patch_file", "write_file_append",
                 "replace_lines", "run_bash", "run_python"):
        return 0
    return consecutive_reads


# ── Recovery-Saturation (aus tool_executor extrahiert) ──

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



async def _handle_too_large(_dname, _dargs, _dresult, _dtc_call, dtool_msgs,
                              last_too_large_path, round_state, hooks):
    """S13: too-large content -> SPLIT REQUIRED (from execute_tool_round)."""
    last_too_large_path[0] = _dargs.get("path", "")
    round_state.parse_errors += 1
    dtool_msgs.append({"role": "tool", "content": _dresult,
                        "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
    if not _recovery_saturated(dtool_msgs):
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            f"[SPLIT REQUIRED] {_dname} rejected: content too large.\n"
            f"You MUST split the file into chunks:\n"
            f"  1. edit_file(path, <first segment>)\n"
            f"  2. write_file_append(path, <next segment>)\n"
            f"  3. write_file_append(path, <rest...>)\n"
            f"Do NOT call edit_file again with the same large content. Start chunk 1 now."
        )})
    await hooks.emit({"type": "token", "content": f"\n[⚠ {_dname}: too large — forcing split mode]\n"})

def _inject_tool_error_hints(_dname, _dargs, _dresult, _dtc_call, dtool_msgs,
                              attempts_per_file, tool_error_retries) -> tuple[str, bool]:
    """U2: tool-specific error hints (from execute_tool_round).
    Returns (new _dresult, matched).
    """
    _matched = False
    if _dname == "edit_file" and _tool_error_has_code(_dresult, "EDIT_FILE_MALFORMED_BLOCK", _dname):
        _matched = True
        _ei_path = _dargs.get("path", "")
        tool_error_retries[_ei_path] = tool_error_retries.get(_ei_path, 0) + 1
        if tool_error_retries[_ei_path] >= 4:
            _dresult = (
                f"[SYSTEM] edit_file on '{_ei_path}' failed format 3x.\n"
                f"Correct SEARCH/REPLACE format:\n"
                f"<<<<<<< SEARCH\n<exact old code>\n=======\n"
                f"<new code>\n>>>>>>> REPLACE\n"
                f"The SEARCH text must match the code in '{_ei_path}' EXACTLY. "
                f"Read the file with read_file to see the exact text."
            )
        else:
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
            if tool_error_retries[_ei_path] >= 2 and not _recovery_saturated(dtool_msgs):
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"[FORMAT ERROR] edit_file requires SEARCH/REPLACE blocks. Use this EXACT format:\n"
                    f"<<<<<<< SEARCH\nold code line 1\nold code line 2\n"
                    f"=======\nnew code line 1\nnew code line 2\n"
                    f">>>>>>> REPLACE\n"
                    f"Repeat for each edit. The SEARCH text must match EXACTLY the existing code."
                )})
    elif _dname == "patch_file" and _tool_error_has_code(_dresult, "PATCH_FILE_OLD_STR_NOT_FOUND", "patch_file"):
        _matched = True
        _pf_path = _dargs.get("path", "")
        _ps_key = f"pf_not_found:{_pf_path}"
        attempts_per_file[_ps_key] = attempts_per_file.get(_ps_key, 0) + 1
        if attempts_per_file[_ps_key] >= 3:
            _dresult = (
                f"[SYSTEM] patch_file repeatedly failing on '{_pf_path}'. "
                f"Switch strategy: use edit_file with the full corrected block instead."
            )
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        else:
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
            if attempts_per_file[_ps_key] >= 2 and not _recovery_saturated(dtool_msgs):
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"PATCH_FILE_OLD_STR_NOT_FOUND: your old_str did not match exactly.\n"
                    f"Call read_file on this path first, then retry with the exact "
                    f"characters from the file — including whitespace and indentation."
                )})
    elif _dname == "patch_file" and _tool_error_has_code(_dresult, "PATCH_FILE_NON_UNIQUE_MATCH", "patch_file"):
        _matched = True
        _pf_path = _dargs.get("path", "")
        _ps_key = f"pf_non_unique:{_pf_path}"
        attempts_per_file[_ps_key] = attempts_per_file.get(_ps_key, 0) + 1
        if attempts_per_file[_ps_key] >= 2:
            _dresult = (
                f"[SYSTEM] patch_file still non-unique on '{_pf_path}'. "
                f"Switch to edit_file with complete block content instead."
            )
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        else:
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
            if not _recovery_saturated(dtool_msgs):
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"PATCH_FILE_NON_UNIQUE_MATCH: old_str matched multiple locations.\n"
                    f"Add more surrounding lines to make it unique, then retry."
                )})
    elif _dname == "edit_file" and _tool_error_has_code(_dresult, "EDIT_FILE_NO_BLOCKS_APPLIED", "edit_file"):
        _matched = True
        _pf_path = _dargs.get("path", "")
        _ws = Path(workspace_lock) if workspace_lock else Path(os.environ.get("HIVEMIND_WORKSPACE", "."))
        _fp = _ws / _pf_path if not Path(_pf_path).is_absolute() else Path(_pf_path)
        _is_empty = not _fp.exists() or _fp.stat().st_size == 0
        _nb_key = f"nb_{_pf_path}"
        attempts_per_file[_nb_key] = attempts_per_file.get(_nb_key, 0) + 1
        if attempts_per_file[_nb_key] >= 2:
            _dresult = (
                f"[SYSTEM] Repeated EDIT_FILE_NO_BLOCKS_APPLIED on '{_pf_path}'. "
                f"Stop using edit_file here. Use write_file with the complete "
                f"file content as plain text — no SEARCH/REPLACE markers."
            )
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        else:
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
            if _is_empty:
                if not _recovery_saturated(dtool_msgs):
                    dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                        f"EDIT_FILE_NO_BLOCKS_APPLIED on '{_pf_path}': file is new or empty — "
                        f"edit_file requires existing content to match. "
                        f"Use write_file with plain content instead (no SEARCH/REPLACE markers)."
                    )})
            else:
                if not _recovery_saturated(dtool_msgs):
                    dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                        f"edit_file cannot apply to this content on '{_pf_path}'.\n"
                        f"Call write_file NOW with the complete correct file content.\n"
                        f"Do NOT call edit_file again on this path.\n"
                        f"Do NOT generate explanatory text — call write_file immediately."
                    )})
    elif _dname == "edit_file" and _tool_error_has_code(_dresult, "EDIT_FILE_NOOP", "edit_file"):
        _matched = True
        _no_path = _dargs.get("path", "")
        _no_key = f"noop:{_no_path}"
        attempts_per_file[_no_key] = attempts_per_file.get(_no_key, 0) + 1
        if attempts_per_file[_no_key] >= 3:
            _dresult = (
                f"[SYSTEM] edit_file produced no change on '{_no_path}' 3x. "
                f"Your SEARCH and REPLACE are identical — the file is NOT modified. "
                f"Call read_file('{_no_path}') to see the current content, then send "
                f"a REPLACE block containing the NEW code."
            )
        else:
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
            if not _recovery_saturated(dtool_msgs):
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"EDIT_FILE_NOOP on '{_no_path}': SEARCH and REPLACE were identical — "
                    f"no change was made. Read the file again, then provide the actual "
                    f"new code in the REPLACE section (different from SEARCH)."
                )})
    elif _tool_error_has_code(_dresult, "RUN_BASH_TIMEOUT", "run_bash"):
        _matched = True
        tool_error_retries["RUN_BASH_TIMEOUT"] = tool_error_retries.get("RUN_BASH_TIMEOUT", 0) + 1
        if tool_error_retries["RUN_BASH_TIMEOUT"] >= 2:
            _dresult = (
                f"[SYSTEM] run_bash timed out {tool_error_retries['RUN_BASH_TIMEOUT']}x. "
                f"The command is too expensive. Split it into smaller steps, "
                f"use run_python for logic, or check if an existing faster tool "
                f"(find_files, search_code, read_file) can achieve the same result.\n\n"
                f"Last result: {_dresult[:600]}"
            )
        else:
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
            if not _recovery_saturated(dtool_msgs):
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"[TIMEOUT] run_bash timed out. The command is too slow or hung. "
                    f"Do NOT retry the identical command — it will time out again. "
                    f"Check if you can: (a) use a faster built-in tool, "
                    f"(b) split into smaller steps, or (c) use run_python instead."
                )})
    elif _tool_error_has_code(_dresult, "RUN_BASH_NONZERO", "run_bash"):
        _matched = True
        tool_error_retries["RUN_BASH_NONZERO"] = tool_error_retries.get("RUN_BASH_NONZERO", 0) + 1
        if tool_error_retries["RUN_BASH_NONZERO"] >= 4:
            _dresult = (
                f"[SYSTEM] run_bash produced a non-zero exit code 4x. "
                f"The command or its approach does not work. "
                f"Change strategy — use Python (run_python), "
                f"access files directly (read_file/edit_file), "
                f"or analyze the root cause before continuing.\n\n"
                + ('...' if len(_dresult) > 300 else '')
            )
        else:
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
            if tool_error_retries["RUN_BASH_NONZERO"] >= 2 and not _recovery_saturated(dtool_msgs):
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"[SYSTEM] run_bash returned non-zero exit code "
                    f"(attempt {tool_error_retries['RUN_BASH_NONZERO']}/4). "
                    f"Do NOT retry the exact same command. Read documentation, "
                    f"check file contents, or use a different approach."
                )})
    elif _tool_error_has_code(_dresult, "RUN_BASH_BLOCKED", "run_bash"):
        _matched = True
        tool_error_retries["RUN_BASH_BLOCKED"] = tool_error_retries.get("RUN_BASH_BLOCKED", 0) + 1
        if tool_error_retries["RUN_BASH_BLOCKED"] >= 2:
            _dresult = (
                f"[SYSTEM] run_bash blocked 2x — this or similar commands "
                f"are blocked for security reasons. Use Python (run_python) "
                f"or git commands as an alternative."
            )
        else:
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
            if not _recovery_saturated(dtool_msgs):
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"[BLOCKED] run_bash command blocked for safety. "
                    f"Do NOT retry the same command. Use run_python or a safer alternative."
                )})
    elif _tool_error_has_code(_dresult, "RUN_BASH_EXEC_ERROR", "run_bash"):
        _matched = True
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        if not _recovery_saturated(dtool_msgs):
            dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                "run_bash: the shell itself failed to execute (not the command). "
                "Try run_python for the same logic, or simplify to a single build/test command. "
                "Do NOT retry the identical run_bash call."
            )})
    elif _tool_error_has_code(_dresult, "FILE_READ_FAILED", "read_file"):
        _matched = True
        _rf_path = _dargs.get("path", "")
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            f"FILE_READ_FAILED on '{_rf_path}': check the path with list_dir, "
            f"verify the file exists, then retry. If the file is missing, "
            f"use find_files to locate it or create it with write_file."
        )})
    elif _tool_error_has_code(_dresult, "EDIT_AST_UNAVAILABLE", "edit_ast"):
        _matched = True
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "EDIT_AST_UNAVAILABLE: AST editor not available. "
            "Use edit_file with SEARCH/REPLACE blocks as fallback. "
            "Read the file first, then build a SEARCH block with the "
            "exact target function/class to replace."
        )})
    elif _tool_error_has_code(_dresult, "GIT_COMMIT_FAILED", "git_commit"):
        _matched = True
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "GIT_COMMIT_FAILED: run git_status first to check for "
            "conflicts, untracked files, or detached HEAD state, "
            "then resolve and retry the commit."
        )})
    elif _tool_error_has_code(_dresult, "WEBSEARCH_UNAVAILABLE", "web_search") or \
         _tool_error_has_code(_dresult, "WEBSEARCH_FAILED", "web_search"):
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "Web search unavailable or failed. "
            "Use search_code to find patterns in the codebase, "
            "or rely on existing code knowledge. Do NOT retry web_search."
        )})
    elif _tool_error_has_code(_dresult, "WEBFETCH_UNAVAILABLE", "web_fetch") or \
         _tool_error_has_code(_dresult, "WEBFETCH_FAILED", "web_fetch"):
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "Web fetch unavailable or failed. "
            "Use web_search to find the content elsewhere, "
            "or check if local documentation exists with find_files. "
            "Do NOT retry web_fetch with the same URL."
        )})
    elif _tool_error_has_code(_dresult, "GIT_UNAVAILABLE") or \
         _tool_error_has_code(_dresult, "GIT_STATUS_FAILED"):
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "Git tools not available or git status failed — "
            "repo may be damaged or not initialized. "
            "Skip all git operations and use find_files + search_code "
            "to inspect project state. Do NOT retry git commands."
        )})
    elif _tool_error_has_code(_dresult, "TOOL_EXEC_CRASH") or \
         _tool_error_has_code(_dresult, "TOOL_NOT_FOUND") or \
         _tool_error_has_code(_dresult, "TOOL_NOT_ALLOWED"):
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "Tool call failed (crash, unknown tool, or not allowed). "
            "Use only the tools listed in your system prompt function list. "
            "Try a different tool to achieve the same result. "
            "Do NOT retry the same call."
        )})
    elif _tool_error_has_code(_dresult, "FIND_FILES_FAILED", "find_files"):
        _matched = True
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "FIND_FILES_FAILED: try a simpler glob like *.py or *.ts, "
            "or use list_dir to explore the directory manually."
        )})
    elif _tool_error_has_code(_dresult, "LIST_DIR_FAILED", "list_dir"):
        _matched = True
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "LIST_DIR_FAILED: use find_files with **/* pattern "
            "to explore the directory instead."
        )})
    elif _tool_error_has_code(_dresult, "GET_SIGNATURES_FAILED", "get_signatures"):
        _matched = True
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "GET_SIGNATURES_FAILED: use read_file with start_line/end_line "
            "to inspect the file section manually."
        )})
    elif _tool_error_has_code(_dresult, "WRITE_FILE_APPEND_FAILED", "write_file_append"):
        _matched = True
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "WRITE_FILE_APPEND_FAILED: append failed (disk full or permission). "
            "Use write_file to fully overwrite the target path instead."
        )})
    elif _tool_error_has_code(_dresult, "REPLACE_LINES_INVALID_START", "replace_lines") or \
         _tool_error_has_code(_dresult, "REPLACE_LINES_INVALID_END", "replace_lines") or \
         _tool_error_has_code(_dresult, "REPLACE_LINES_FAILED", "replace_lines"):
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "REPLACE_LINES error: line numbers may be stale. "
            "Call read_file to get current line numbers, then retry. "
            "If that fails, use edit_file with SEARCH/REPLACE instead."
        )})
    elif _tool_error_has_code(_dresult, "MISSING_ARG", "git_commit"):
        _matched = True
        dtool_msgs.append({"role": "tool", "content": _dresult,
                            "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
            "MISSING_ARG: git_commit requires a non-empty message. "
            "Provide a short one-line description of the changes."
        )})
    elif _tool_error_has_code(_dresult, "INVALID_ARGUMENT"):
        _matched = True
        _ia_key = f"ia_{_dname}"
        attempts_per_file[_ia_key] = attempts_per_file.get(_ia_key, 0) + 1
        if attempts_per_file[_ia_key] >= 2:
            _dresult = (
                f"[SYSTEM] Repeated INVALID_ARGUMENT on {_dname}. "
                f"Do not call {_dname} again until you have verified "
                f"the exact argument schema. Use a simpler tool if unsure."
            )
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
        else:
            dtool_msgs.append({"role": "tool", "content": _dresult,
                                "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
            if not _recovery_saturated(dtool_msgs):
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    f"INVALID_ARGUMENT on {_dname}. "
                    f"Check the required arguments for {_dname} and retry "
                    f"with all required fields correctly formatted."
                )})
    return _dresult, _matched

# ── Batch 3: emit-heavy sections (from execute_tool_round) ─────────────────

async def _handle_ask_user(_dargs, hooks, exec_model, cached_coder_port, run_id_global):
    """S6: ask_user - evict_model + agent_asking/status emits (from execute_tool_round)."""
    if hooks.evict_model is not None:
        try:
            await hooks.evict_model(exec_model)
            cached_coder_port[0] = None
        except Exception:
            pass
    # Emit agent_asking for duo_runner (runner.py only emits via _tool_loop_emit)
    # initiate_pause + wait_for_resume handled by runner.py's _handle_ask_user
    await hooks.emit({"type": "agent_asking",
        "question": _dargs.get("question", ""), "run_id": run_id_global})
    await hooks.emit({"type": "status", "content": "⏸️ Agent asks — waiting for the answer…"})

    # ── web_search dedup ──

async def _execute_one_tool(_dname, _dargs, _dargs_with_model, _i, _pre_results,
                              duo_seen_web_queries, workspace_lock, tool_mode, duo_ws) -> str:
    """S7: web_search dedup + tool execution (from execute_tool_round)."""
    if _dname == "web_search":
        _ws_query_norm = str(_dargs.get("query", "")).strip().lower()
        if _ws_query_norm and _ws_query_norm in duo_seen_web_queries:
            _dresult = f"[web_search dedup: duplicate query skipped: {_ws_query_norm[:180]}]"
        else:
            if _ws_query_norm:
                duo_seen_web_queries.add(_ws_query_norm)
            if _i in _pre_results:
                _dresult = _pre_results[_i]
            else:
                _READ_TOOL_TIMEOUT = 30.0
                _READ_TOOLS = {"read_file", "search_code", "find_files", "list_dir",
                               "get_file_summary", "get_signatures"}
                if _dname in _READ_TOOLS:
                    try:
                        _dresult = await asyncio.wait_for(
                            _run_inline_tool(
                                _dname, _dargs_with_model,
                                workspace_lock=workspace_lock,
                                tool_mode=tool_mode, include_websearch=duo_ws),
                            timeout=_READ_TOOL_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        _dresult = _tool_error_response(
                            "TOOL_TIMEOUT",
                            f"{_dname} timed out after {_READ_TOOL_TIMEOUT:.0f}s. "
                            "The file system may be unresponsive. "
                            "Try a different path or use find_files with a narrower pattern.",
                            tool=_dname, mode=tool_mode)
                else:
                    try:
                        _dresult = await _run_inline_tool(
                            _dname, _dargs_with_model,
                            workspace_lock=workspace_lock,
                            tool_mode=tool_mode, include_websearch=duo_ws)
                    except Exception as _tool_exc:
                        _dresult = _tool_error_response(
                            "TOOL_EXEC_CRASH",
                            f"{type(_tool_exc).__name__}: {str(_tool_exc)[:200]}",
                            tool=_dname, mode=tool_mode )
    else:
        if _i in _pre_results:
            _dresult = _pre_results[_i]
        else:
            _READ_TOOL_TIMEOUT = 30.0
            _READ_TOOLS = {"read_file", "search_code", "find_files", "list_dir",
                           "get_file_summary", "get_signatures"}
            if _dname in _READ_TOOLS:
                try:
                    _dresult = await asyncio.wait_for(
                        _run_inline_tool(
                            _dname, _dargs_with_model,
                            workspace_lock=workspace_lock,
                            tool_mode=tool_mode, include_websearch=duo_ws),
                        timeout=_READ_TOOL_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    _dresult = _tool_error_response(
                        "TOOL_TIMEOUT",
                        f"{_dname} timed out after {_READ_TOOL_TIMEOUT:.0f}s.",
                        tool=_dname, mode=tool_mode)
            else:
                try:
                    _dresult = await _run_inline_tool(
                        _dname, _dargs_with_model,
                        workspace_lock=workspace_lock,
                        tool_mode=tool_mode, include_websearch=duo_ws)
                except Exception as _tool_exc:
                    _dresult = _tool_error_response(
                        "TOOL_EXEC_CRASH",
                        f"{type(_tool_exc).__name__}: {str(_tool_exc)[:200]}",
                        tool=_dname, mode=tool_mode )

    return _dresult

async def _maybe_activate_reactive_think(_dname, _dresult, round_state, dtool_msgs,
                                          hooks, tool_think_auto_mode, exec_has_thinking):
    """S9: misclassification + reactive thinking (from execute_tool_round)."""
    _this_call_failed = False
    if _dname == "run_bash":
        _this_call_failed = _run_bash_failed(_dresult)
    elif _dname in ("patch_file", "edit_file", "write_file", "write_file_append"):
        _this_call_failed = _tool_call_failed(_dresult, _dname)
    elif _dname == "run_python":
        _this_call_failed = bool(_dresult and (
            _tool_error_has_code(_dresult, "RUN_PYTHON_EXEC_ERROR", "run_python")
            or _tool_error_has_code(_dresult, "RUN_PYTHON_TIMEOUT", "run_python")
        ))

    if _this_call_failed:
        round_state.tool_fail_streak += 1
        if (not round_state.think_runtime
            and tool_think_auto_mode != "off"
            and exec_has_thinking
            and not round_state.reactive_think_activated):
            _should_activate = False
            if tool_think_auto_mode == "on_fail" and round_state.tool_fail_streak >= 1:
                _should_activate = True
            elif tool_think_auto_mode == "balanced" and round_state.tool_fail_streak >= 2:
                _should_activate = True
            if _should_activate:
                round_state.think_runtime = True
                round_state.reactive_think_activated = True
                if exec_has_thinking:
                    dtool_msgs[:] = [
                        {**m, "content": "<|think_on|>" + m["content"].replace("<|think_off|>", "")
                         if m.get("role") == "system" and "<|think_on|>" not in m["content"]
                         else m["content"]}
                        if m.get("role") == "system" else m
                        for m in dtool_msgs
                    ]
                await hooks.emit({
                    "type": "status",
                    "content": (
                        f"🧠 Reaktiv Thinking zugeschaltet "
                        f"(mode={tool_think_auto_mode}, "
                        f"fail_streak={round_state.tool_fail_streak})"
                    ),
                })
    elif _dname in ("patch_file", "edit_file", "write_file", "write_file_append",
                    "run_bash", "run_python"):
        round_state.tool_fail_streak = 0


async def _run_bash_fail_fix_pass_insight(_dname, _dargs, _dresult, result, hooks,
                                            workspace_lock, subtask_index):
    """S10: run_bash fail→fix→pass insight (from execute_tool_round)."""
    _build_fix_insight = None  # lazy import
    _rb_failed = _run_bash_failed(_dresult)
    if _rb_failed:
        result.last_run_bash_failure = {
            "cmd": str(_dargs.get("cmd", "")).strip(),
            "err": str(_dresult or "")[:260],
            "round": subtask_index + 1,
        }
        result.changed_since_failure = set()
    else:
        result.verify_last_ok_serial = max(result.verify_last_ok_serial, result.verify_mutation_serial)
        if result.last_run_bash_failure and result.changed_since_failure:
            _files_for_hint = sorted(result.changed_since_failure)[:3]
            if _build_fix_insight is None:
                from utils.text import build_fix_insight_sentence as _bfis
                _build_fix_insight = _bfis
            _insight = _build_fix_insight(
                result.last_run_bash_failure.get("cmd", "run_bash"),
                result.last_run_bash_failure.get("err", ""),
                _files_for_hint)
            _insight_sig = _insight.lower()
            if _insight_sig and _insight_sig != result.last_learned_insight_sig:
                _insight_path = _files_for_hint[0] if _files_for_hint else workspace_lock
                if hooks.remember_insight:
                    # ~4 Decay-Zyklen (0.08/Cycle) evictet (MIN 0.10).
                    hooks.remember_insight(_insight, trigger_path=_insight_path, source="critic_fail_fix_loop", relevance_score=0.4)
                result.last_learned_insight_sig = _insight_sig
                await hooks.emit({
                    "type": "status",
                    "content": "🧠 Learned insight saved from fail→fix→pass cycle.",
                })
            result.last_run_bash_failure = None
            result.changed_since_failure = set()


def _patch_file_fallback_hint(_dname, _dargs, _dresult, attempts_per_file) -> str:
    """S11: auto-retry patch_file fallback hint (from execute_tool_round)."""
    _pf_path = _dargs.get("path", "")
    if _tool_call_failed(_dresult, _dname):
        attempts_per_file[_pf_path] = attempts_per_file.get(_pf_path, 0) + 1
        if attempts_per_file[_pf_path] >= 2:
            _dresult += (
                f"\n\n[STATE: patch_file failed {attempts_per_file[_pf_path]}x on this file. "
                f"Switch strategy: use edit_file with the COMPLETE new file content instead. "
                f"Read the file first, then edit_file with all changes applied.]"
            )
    if _tool_error_has_code(_dresult, "PATCH_FILE_EMPTY_OLD_STR", "patch_file"):
        _dresult += (
            "\n\nYou called patch_file with empty arguments. "
            "Call read_file first, then either patch_file with exact old_str "
            "or edit_file with complete new content."
        )

    return _dresult

def _read_required_and_python_hints(_dname, _dargs, _dresult, tool_error_retries) -> str:
    """S12: READ_REQUIRED cap + run_python hints (from execute_tool_round)."""
    if _dname in ("read_file", "write_file", "edit_file", "patch_file", "replace_lines"):
        if "READ_REQUIRED" in _dresult:
            tool_error_retries["READ_REQUIRED_CONSECUTIVE"] = tool_error_retries.get("READ_REQUIRED_CONSECUTIVE", 0) + 1
            if tool_error_retries["READ_REQUIRED_CONSECUTIVE"] >= 3:
                _rr_path = _dargs.get("path", "")
                _dresult = (
                    f"[SYSTEM] Loop detected: 3+ READ_REQUIRED errors. "
                    f"You must call read_file('{_rr_path}') before editing it. "
                    f"Read the file content first, then call edit_file. Do not skip the read step."
                )
        elif "[HINT:" in _dresult or "[SKIP:" in _dresult or "no read_file needed" in _dresult:
            pass
        else:
            tool_error_retries["READ_REQUIRED_CONSECUTIVE"] = 0

    if _dname == "run_python" and _tool_error_has_code(_dresult, "RUN_PYTHON_EXEC_ERROR", "run_python"):
        _dresult = f"{_dresult}\n\nThe code has errors. Fix them and call run_python again with the corrected code."

    if _dname == "run_python" and _tool_error_has_code(_dresult, "RUN_PYTHON_TIMEOUT", "run_python"):
        _dresult = (
            f"{_dresult}\n\nPython execution timed out after 10s — likely an infinite loop or excessively "
            f"slow computation. Do NOT re-run the same code. Check for: missing termination conditions, "
            f"unbounded recursion, or operations that can be optimized/replaced."
        )

    return _dresult

def _unknown_error_hint(_dname, _dresult, _dtc_call, dtool_msgs, tool_error_retries):
    """U3: unknown error - retry counter + hint (from execute_tool_round)."""
    dtool_msgs.append({"role": "tool", "content": _dresult,
                        "tool_call_id": _dtc_call.get("id", _dname), "name": _dname})
    if _tool_call_failed(_dresult, _dname):
        tool_error_retries["unknown_error_consecutive"] = tool_error_retries.get("unknown_error_consecutive", 0) + 1
        if tool_error_retries["unknown_error_consecutive"] >= 3:
            if not _recovery_saturated(dtool_msgs):
                dtool_msgs.append({"role": "user", "content": (_SYS_PREFIX +
                    "[REPEATED ERROR] An unrecognized error has occurred 3 times. "
                    "Your current approach is not working. Switch to a completely "
                    "different tool or strategy to achieve the same result."
                )})
            tool_error_retries["unknown_error_consecutive"] = 0
    else:
        tool_error_retries["unknown_error_consecutive"] = 0


async def _track_file_changes(_dname, _dargs, _dresult, result, file_changes,
                               dtool_msgs, hooks, _is_git_repo):
    """S15: file-change tracking incl. git auto-diff (from execute_tool_round)."""
    _fc_path = _dargs.get("path", "")
    if _fc_path and not _tool_call_failed(_dresult, _dname):
        _fc_content = ""
        try:
            _ws_root = Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve()
            _abs = (_ws_root / _fc_path).resolve()
            if str(_abs).startswith(str(_ws_root)):
                _fc_size = _abs.stat().st_size
                if _fc_size <= 100 * 1024:
                    _fc_content = _abs.read_text(encoding="utf-8", errors="replace")
        except Exception:
            _fc_content = ""
        if _dname == "write_file":
            _fc_lines = _fc_content.count("\n") + 1 if _fc_content else _dargs.get("content", "").count("\n") + 1
            file_changes[_fc_path] = {"op": "write", "lines": _fc_lines}
            await hooks.emit({"type": "file_change", "path": _fc_path,
                              "op": "write", "lines": _fc_lines, "content": _fc_content})
            if result.last_run_bash_failure:
                result.changed_since_failure.add(_fc_path)
            if _is_git_repo:
                try:
                    _diff_proc = await asyncio.create_subprocess_exec(
                        "git", "diff", "--stat", "HEAD", "--", str(_fc_path),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        cwd=str(_ws_root))
                    _diff_out, _ = await asyncio.wait_for(_diff_proc.communicate(), 3)
                    _diff_str = _diff_out.decode(errors="replace").strip()
                    if _diff_str:
                        dtool_msgs.append({"role": "tool", "content": f"[auto-diff] {_diff_str}"})
                except Exception:
                    pass
        elif _dname == "edit_file":
            # Parse unified edit_file/write_file result: created / rewrote / edited
            _cr = re.search(r"\[(?:edit_file|write_file): created '[^']+' \(\+(\d+) lines\)\]", _dresult)
            _rw = re.search(r"\[(?:edit_file|write_file): rewrote '[^']+' \(\+(\d+)/-(\d+) lines\)\]", _dresult)
            _bl = re.search(r"\[(?:edit_file|write_file): '[^']+' - (\d+)/(\d+) blocks applied \(([+-]\d+) lines\)\]", _dresult)
            if _cr:
                _added = int(_cr.group(1))
                file_changes[_fc_path] = {"op": "created", "lines_added": _added, "lines_removed": 0}
                await hooks.emit({"type": "file_change", "path": _fc_path,
                                  "op": "created", "lines_added": _added, "content": _fc_content})
            elif _rw:
                _added = int(_rw.group(1))
                _removed = int(_rw.group(2))
                file_changes[_fc_path] = {"op": "rewrote", "lines_added": _added, "lines_removed": _removed}
                await hooks.emit({"type": "file_change", "path": _fc_path,
                                  "op": "rewrote", "lines_added": _added, "lines_removed": _removed, "content": _fc_content})
            elif _bl:
                _applied = int(_bl.group(1))
                _delta = int(_bl.group(3))
                _added = max(0, _delta)
                _removed = abs(min(0, _delta))
                file_changes[_fc_path] = {"op": "edited", "blocks": _applied, "lines_added": _added, "lines_removed": _removed}
                await hooks.emit({"type": "file_change", "path": _fc_path,
                                  "op": "edited", "blocks": _applied, "lines_added": _added, "lines_removed": _removed, "content": _fc_content})
            if result.last_run_bash_failure:
                result.changed_since_failure.add(_fc_path)
            if _is_git_repo:
                try:
                    _diff_proc = await asyncio.create_subprocess_exec(
                        "git", "diff", "--stat", "HEAD", "--", str(_fc_path),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        cwd=str(_ws_root))
                    _diff_out, _ = await asyncio.wait_for(_diff_proc.communicate(), 3)
                    _diff_str = _diff_out.decode(errors="replace").strip()
                    if _diff_str:
                        dtool_msgs.append({"role": "tool", "content": f"[auto-diff] {_diff_str}"})
                except Exception:
                    pass
        elif _dname == "write_file_append":
            _fc_lines = _dargs.get("content", "").count("\n") + 1
            _prev = file_changes.get(_fc_path, {"op": "append", "lines_added": 0, "lines_removed": 0})
            _added = _prev.get("lines_added", 0) + _fc_lines
            file_changes[_fc_path] = {"op": "append", "lines_added": _added, "lines_removed": 0}
            await hooks.emit({"type": "file_change", "path": _fc_path,
                              "op": "append", "lines_added": _fc_lines, "content": _fc_content})
            if result.last_run_bash_failure:
                result.changed_since_failure.add(_fc_path)
        elif _dname == "patch_file":
            _pf_m = re.search(r"(\d+)/(\d+) blocks applied", _dresult)
            file_changes[_fc_path] = {"op": "edited", "blocks": int(_pf_m.group(1)) if _pf_m else 1,
                                       "lines_added": 0, "lines_removed": 0}
            await hooks.emit({"type": "file_change", "path": _fc_path,
                              "op": "edited",
                              "blocks": int(_pf_m.group(1)) if _pf_m else 1,
                              "content": _fc_content})
            if result.last_run_bash_failure:
                result.changed_since_failure.add(_fc_path)

def _register_context_lru(dtool_msgs, tool_ctx_lru, _focus_path, _dname, _dresult):
    """S16: context LRU registration (from execute_tool_round)."""
    _tool_msg_idx = -1
    if dtool_msgs and dtool_msgs[-1].get("role") == "tool":
        _tool_msg_idx = len(dtool_msgs) - 1
    elif len(dtool_msgs) >= 2 and dtool_msgs[-2].get("role") == "tool":
        _tool_msg_idx = len(dtool_msgs) - 2
    if _tool_msg_idx >= 0:
        from context.compression import _weighted_ttl
        tool_ctx_lru.register(
            _tool_msg_idx,
            path=_focus_path,
            size_chars=len(str(_dresult or "")),
            kind=_dname,
            ttl_override=_weighted_ttl(str(_dresult or ""), tool_ctx_lru.default_ttl))


@dataclass
class ToolRoundState:
    """Bundled round state (17 mutable slots) — audit point
    "36 params / mutable-slot boxing": the slots are passed as one object
    instead of 17 individual parameters."""
    tool_ctx_lru: object = None
    duo_deadline_at: float = 0.0
    verify_mutation_serial: int = 0
    verify_last_ok_serial: int = 0
    last_run_bash_failure: dict | None = None
    changed_since_failure: set = field(default_factory=set)
    last_learned_insight_sig: str = ""
    last_too_large_path: list = field(default_factory=lambda: [None])
    attempts_per_file: dict = field(default_factory=dict)
    tool_error_retries: dict = field(default_factory=dict)
    call_sigs: list = field(default_factory=list)
    recent_focus_paths: list = field(default_factory=list)
    file_changes: dict = field(default_factory=dict)
    duo_seen_web_queries: set = field(default_factory=set)
    cached_coder_port: list = field(default_factory=lambda: [None])
    task_complete_blocked_count: list = field(default_factory=lambda: [0])
    total_tool_errors: list = field(default_factory=lambda: [0])
