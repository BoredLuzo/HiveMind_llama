"""Tool runner and path utilities (extracted from server.py)."""
from __future__ import annotations
import contextvars as _contextvars
import os
import re
from pathlib import Path

from tools.errors import tool_error_response as _tool_error_response
from tools.definitions import _tool_names_for_mode
from tools.workspace import get_transaction
from utils.file import fuzzy_resolve_path as _fuzzy_resolve_path, _inline_resolve_path, _inline_check_workspace, normalize_tool_path as _normalize_tool_path
from tools.handlers import (
    _inline_tool_read_file,
    _inline_tool_get_signatures,
    _inline_tool_find_references,
    _inline_tool_edit_ast,
    _inline_tool_list_dir,
    _inline_tool_search_code,
    _inline_tool_run_python,
    _inline_tool_git_status,
    _inline_tool_git_commit,
    _inline_tool_subagent_research,
    _inline_tool_find_files,
    _inline_tool_run_bash,
    _inline_tool_write_file,
    _inline_tool_write_file_append,
    _inline_tool_patch_file,
    _inline_tool_edit_file,
    _inline_tool_replace_lines,
    _inline_tool_undo_last,
    _inline_tool_web_search,
    _inline_tool_web_fetch,
    _inline_tool_install_package,
    _inline_tool_run_tests,
    _inline_tool_start_background,
    _inline_tool_get_background_output,
    _inline_tool_stop_background,
    _inline_tool_get_datetime,
    _inline_tool_task_complete)

from tools.browser import browser_tool as _browser_tool

# Phase A.3: Read-Guard ContextVar (in server.py als Module-Level definiert)
_files_read_in_run: _contextvars.ContextVar[set] = _contextvars.ContextVar(
    "_files_read_in_run", default=None
)
_files_in_context: _contextvars.ContextVar[set] = _contextvars.ContextVar(
    "_files_in_context", default=None
)
_files_seen_in_run: _contextvars.ContextVar[set] = _contextvars.ContextVar(
    "_files_seen_in_run", default=None
)
_files_written_in_run: _contextvars.ContextVar[set] = _contextvars.ContextVar(
    "_files_written_in_run", default=None
)

_current_project_state: _contextvars.ContextVar = _contextvars.ContextVar(
    "current_project_state", default=None
)

# reserved for the internal coder loop.
_external_dispatch: _contextvars.ContextVar = _contextvars.ContextVar(
    "_external_dispatch", default=False
)


def _is_external_dispatch() -> bool:
    return bool(_external_dispatch.get(False))


def _read_guard_enabled() -> bool:


    try:
        from core.state import settings as _ws_settings
        return bool(_ws_settings.get("read_guard_enabled", True))
    except Exception:
        return True


def reset_read_guard_after_compression():


    import logging as _logging
    _logging.getLogger("hivemind.tools").debug(
        "[READ-GUARD] Compression: Read-Guard-Sets behalten (Keep statt Clear — False-Positive-Fix)"
    )


# DROPPED → Loop). ContextVar propagiert in asyncio.create_task-Child-Tasks.
_write_budget_cv: _contextvars.ContextVar = _contextvars.ContextVar(
    "_write_budget_cv", default=None
)


def _set_write_budget(token_budget: int | None = None, chars_per_token: float | None = None):
    _write_budget_cv.set((token_budget, chars_per_token))


def _get_write_budget():
    return _write_budget_cv.get()


# pause/resume: run_id bridge from duo_runner to the ask_user handler
_current_run_id: _contextvars.ContextVar[str] = _contextvars.ContextVar(
    "current_run_id", default=""
)
_pause_timeout_s: _contextvars.ContextVar[int] = _contextvars.ContextVar(
    "pause_timeout_s", default=600
)
_tool_loop_emit: _contextvars.ContextVar = _contextvars.ContextVar(
    "tool_loop_emit", default=None
)
# ask_user throttle: "open" | "throttled_retries" | "throttled_autonomous"
_ask_user_gate: _contextvars.ContextVar[str] = _contextvars.ContextVar(
    "ask_user_gate", default="open"
)
# ask_user throttle counter: blocks after N throttled calls
_ask_user_throttled_count: _contextvars.ContextVar[int] = _contextvars.ContextVar(
    "ask_user_throttled_count", default=0
)

# A-P1-9: web-search call budget (governor analogous to ask_user). Mutable list [n]
_web_search_count: _contextvars.ContextVar[list] = _contextvars.ContextVar(
    "web_search_count", default=None
)
_WEBSEARCH_BUDGET_DEFAULT = 20


def _consume_web_search_budget() -> bool:

    try:
        from core.state import settings as _ws_settings
        _limit = int(_ws_settings.get("duo_websearch_max_calls", _WEBSEARCH_BUDGET_DEFAULT))
    except Exception:
        _limit = _WEBSEARCH_BUDGET_DEFAULT
    _c = _web_search_count.get(None)
    if _c is None:
        _c = [0]
        _web_search_count.set(_c)
    _c[0] += 1
    return _c[0] > _limit


# A-P1-6: Dependency-Install-Call-Budget (Governor analog web_search). Install-
_install_count: _contextvars.ContextVar[list] = _contextvars.ContextVar(
    "install_count", default=None
)
_INSTALL_BUDGET_DEFAULT = 3


def _consume_install_budget() -> bool:

    try:
        from core.state import settings as _is_settings
        _limit = int(_is_settings.get("duo_install_max_calls", _INSTALL_BUDGET_DEFAULT))
    except Exception:
        _limit = _INSTALL_BUDGET_DEFAULT
    _c = _install_count.get(None)
    if _c is None:
        _c = [0]
        _install_count.set(_c)
    _c[0] += 1
    return _c[0] > _limit

# Phase-1 instrumentation: per-run counters + structured logs
# for ask_user / destructive_gate (logging only, no behavior impact).
_TOOL_USE_COUNTS: dict = {}


def _note_tool_use(run_id: str, tool: str, outcome: str) -> None:
    import logging as _tl
    _bucket = _TOOL_USE_COUNTS.setdefault(str(run_id), {})
    _k = f"{tool}:{outcome}"
    _bucket[_k] = _bucket.get(_k, 0) + 1
    _tl.getLogger("hivemind.tools").info(
        "[TOOL-USE] run=%s tool=%s outcome=%s total=%d",
        run_id, tool, outcome, _bucket[_k],
    )


# in tool_call-arguments -> ungueltiges JSON -> llama-server 500 "Failed to parse
_RE_META_WINPATH = re.compile(r"[A-Za-z]:\\[^\s'\"\]\n)]*")


def _normalize_meta_paths(text: str) -> str:


    out = []
    for _ln in text.split("\n"):
        if _ln.lstrip().startswith("[") or "BLOCKED on" in _ln:
            _ln = _RE_META_WINPATH.sub(lambda m: m.group(0).replace("\\", "/"), _ln)
        out.append(_ln)
    return "\n".join(out)


async def _handle_ask_user(args: dict, workspace, workspace_lock) -> str:
    _gate = _ask_user_gate.get("open")
    question = args.get("question", "")
    _note_tool_use(_current_run_id.get(), "ask_user", "requested")
    import logging as _ask_diag_log
    _ask_diag_log.getLogger("hivemind.tools").info(
        "[ASK-DIAG] enter gate=%s run_id=%s qlen=%d",
        _gate, _current_run_id.get(), len(question or ""))

    def _save_project_state():
        try:
            _psv = _current_project_state.get()
            if _psv is not None:
                from context.project_state import ProjectStateManager
                ProjectStateManager().save(_psv)
        except Exception as _pps_err:
            import logging as _pps_log
            _pps_log.getLogger("hivemind.tools").warning(
                "[PROJECT] Pre-Pause-Save fehlgeschlagen: %s", _pps_err)

    if _gate == "throttled_autonomous":
        _count = _ask_user_throttled_count.get(0) + 1
        _ask_user_throttled_count.set(_count)
        if _count >= 3:
            return (
                "[ASK_USER_BLOCKED] You have called ask_user "
                f"{_count} times while throttled. "
                "Stop calling this tool and proceed autonomously."
            )
        return (
            "[ASK_USER_THROTTLED] In autonomous mode. "
            "Make your best attempt. Only escalate after "
            "2 failed fix attempts."
        )
    if _gate == "throttled_retries":
        _count = _ask_user_throttled_count.get(0) + 1
        _ask_user_throttled_count.set(_count)
        if _count >= 3:
            return (
                "[ASK_USER_BLOCKED] You have called ask_user "
                f"{_count} times while throttled. "
                "Stop calling this tool and proceed autonomously."
            )
        return (
            "[ASK_USER_THROTTLED] Retries not exhausted. "
            "Attempt fix autonomously first."
        )

    run_id = _current_run_id.get()
    if not run_id:
        return _tool_error_response(
            "ASK_USER_NO_RUN_ID",
            "no run_id in context — ask_user requires an active run context.",
            tool="ask_user" )
    _emit = _tool_loop_emit.get(None)
    from infra import run_control
    from infra.ask_user_governor import (
        get_run_config as _gov_config, record_ask_user as _gov_record,
        check_throttle as _gov_check, is_throttle_triggered as _gov_is_throttled,
        set_throttle_triggered as _gov_set_throttle, start_timeout as _gov_start_timeout,
        is_timeout_answer_sent as _gov_timeout_sent,
    )
    _gcfg = _gov_config(run_id)
    if _gcfg:
        _gov_record(run_id)
        _exceeded, _ask_count = _gov_check(run_id, _gcfg["max_per_10min"])
        if _exceeded and not _gov_is_throttled(run_id):
            _gov_set_throttle(run_id)
            if _emit:
                try:
                    await _emit({"type": "agent_throttled", "run_id": run_id,
                                 "question": question, "ask_user_count": _ask_count,
                                 "message": _gcfg["throttle_message"]})
                    await _emit({"type": "status",
                                 "content": "\u26a0 Agent is asking too many questions \u2014 manual help required"})
                    try:
                        from infra.notify import notify_agent_needs_input
                        notify_agent_needs_input(str(run_id or ""), "Agent is asking too many questions")
                    except Exception:
                        pass
                except Exception:
                    pass
            _save_project_state()
            await run_control.initiate_pause(run_id, _gcfg["throttle_message"])
            _t_answer = await run_control.wait_for_resume(run_id, timeout_s=3600)
            if _emit:
                try:
                    await _emit({"type": "agent_resumed"})
                except Exception:
                    pass
            return _t_answer
    if _emit:
        try:
            await _emit({"type": "agent_asking", "question": question, "run_id": run_id})
            await _emit({"type": "status", "content": "Agent asks — waiting for answer\u2026"})
            try:
                from infra.notify import notify_agent_needs_input
                notify_agent_needs_input(str(run_id or ""), str(question or ""))
            except Exception:
                pass
        except Exception:
            pass
    _save_project_state()
    await run_control.initiate_pause(run_id, question)
    if _gcfg and _gcfg["until_finished"] and _gcfg["timeout_s"] > 0:
        await _gov_start_timeout(run_id, _gcfg["timeout_s"], _gcfg["auto_answer"])
    timeout = _pause_timeout_s.get()
    import logging as _ask_diag2, time as _ask_diag_t
    import asyncio as _ask_diag_ai
    _ask_diag2.getLogger("hivemind.tools").warning(
        "[ASK-DIAG] pausing run_id=%s gate=%s qlen=%d pause_event_present=%s",
        run_id, _gate, len(question or ""),
        bool(run_control.get_question(run_id)),
    )
    _ask_t0 = _ask_diag_t.monotonic()
    try:
        answer = await run_control.wait_for_resume(run_id, timeout_s=timeout)
    except _ask_diag_ai.CancelledError:
        _ask_diag2.getLogger("hivemind.tools").warning(
            "[ASK-DIAG] wait_for_resume CANCELLED run_id=%s after %.1fs",
            run_id, _ask_diag_t.monotonic() - _ask_t0)
        raise
    except Exception as _ask_exc:
        _ask_diag2.getLogger("hivemind.tools").warning(
            "[ASK-DIAG] wait_for_resume ERROR run_id=%s: %s",
            run_id, _ask_exc)
        answer = ""
    _ask_diag2.getLogger("hivemind.tools").warning(
        "[ASK-DIAG] resumed run_id=%s after %.1fs answer_len=%d",
        run_id, _ask_diag_t.monotonic() - _ask_t0, len(answer or ""))
    _note_tool_use(
        run_id, "ask_user",
        "paused_timeout"
        if not answer or not answer.strip() or str(answer).startswith("[ask_user")
        else "paused_answered",
    )
    if _gcfg and _gov_timeout_sent(run_id):
        if _emit:
            try:
                await _emit({"type": "ask_user_timeout_reached", "run_id": run_id,
                             "timeout_seconds": _gcfg["timeout_s"], "auto_answer": answer})
            except Exception:
                pass
    if not answer or not answer.strip():
        answer = (
            f"[ask_user: no response after {timeout}s "
            f"\u2014 proceeding with best judgment]"
        )
    if _emit:
        try:
            await _emit({"type": "agent_resumed"})
        except Exception:
            pass
    return answer


_INLINE_TOOL_HANDLER_MAP = {
    "read_file": _inline_tool_read_file,
    "get_signatures": _inline_tool_get_signatures,
    "find_references": _inline_tool_find_references,
    "list_dir": _inline_tool_list_dir,
    "search_code": _inline_tool_search_code,
    "run_bash": _inline_tool_run_bash,
    "run_python": _inline_tool_run_python,
    "git_status": _inline_tool_git_status,
    "find_files": _inline_tool_find_files,
    "write_file": _inline_tool_write_file,
    "write_file_append": _inline_tool_write_file_append,
    "patch_file": _inline_tool_patch_file,
    "edit_file": _inline_tool_edit_file,
    "replace_lines": _inline_tool_replace_lines,
    "edit_ast": _inline_tool_edit_ast,
    "undo_last": _inline_tool_undo_last,
    "web_search": _inline_tool_web_search,
    "web_fetch": _inline_tool_web_fetch,
    "install_package": _inline_tool_install_package,
    "run_tests": _inline_tool_run_tests,
    "start_background": _inline_tool_start_background,
    "get_background_output": _inline_tool_get_background_output,
    "stop_background": _inline_tool_stop_background,
    "git_commit": _inline_tool_git_commit,
    "subagent_research": _inline_tool_subagent_research,
    "get_datetime": _inline_tool_get_datetime,
    "ask_user": _handle_ask_user,
    "task_complete": _inline_tool_task_complete,
    "browser": _browser_tool,
}

# ── 2.9 ZENTRALER ENFORCEMENT-FUNNEL (2026-08-24) ─────────────────────────────
#
# Regeln:
#   - Legitime Intra-Modul-Delegationen (z.B. write_file → edit_file in
_dispatch_active_cv: _contextvars.ContextVar = _contextvars.ContextVar(
    "_dispatch_active", default=False
)


def _funnel_guard(name: str, fn):
    async def _guarded(args: dict, workspace: Path, workspace_lock: str | None) -> str:
        if not _dispatch_active_cv.get(False):
            return _tool_error_response(
                "FUNNEL_BYPASS_BLOCKED",
                f"Tool '{name}' was called outside the central dispatch funnel. "
                "All calls must go through tools.runner._run_inline_tool.",
                tool=name,
            )
        return await fn(args, workspace, workspace_lock)
    _guarded.__name__ = getattr(fn, "__name__", name)
    return _guarded


_INLINE_TOOL_HANDLER_MAP = {
    _name: _funnel_guard(_name, _fn)
    for _name, _fn in _INLINE_TOOL_HANDLER_MAP.items()
}


_DESTRUCTIVE_BASH_PATTERNS = [
    # PowerShell
    r"Remove-Item\s.*-[Rr]ecurse.*-[Ff]orce",
    r"Remove-Item\s.*-[Ff]orce.*-[Rr]ecurse",
    r"\brm\s+-rf?\b",
    r"\brm\s+.*-r\s.*-f\b",
    r"\bdel\s+/[Ff]\s+/[Ss]\s+/[Qq]\b",
    r"\bformat\s+[A-Za-z]:",
    r"\bdiskpart\b",
    # Git destructive
    r"git\s+reset\s+--hard",
    r"git\s+push\s+--force",
    r"git\s+push\s+--force-with-lease",
    r"git\s+clean\s+-fdx",
    # SQL destructive
    r"\bDROP\s+TABLE\b",
    r"\bDROP\s+DATABASE\b",
    r"\bTRUNCATE\b",
    # System
    r"\bStop-Computer\b",
    r"\bRestart-Computer\b",
    r"\bShutdown\b",
    r"\btaskkill\b",
    # Permission changes
    r"\bicacls\b",
    r"\bchmod\s+[-+]\w",
    r"\bcacls\b",
    # Network/registry
    r"\bSet-ItemProperty\b",
    r"\bNew-ItemProperty\b",
    r"\breg\s+(add|delete)\b",
]


def _is_destructive_bash(cmd: str) -> str | None:
    """Return matching pattern description if command is destructive, else None."""
    import re as _dre
    _cmd_lower = str(cmd).lower()
    for _pat in _DESTRUCTIVE_BASH_PATTERNS:
        if _dre.search(_pat, _cmd_lower, _dre.IGNORECASE):
            return _pat
    return None


_DESTRUCTIVE_PYTHON_PATTERNS = [
    # Shell/system access
    r"\bos\.system\s*\(",
    r"\bos\.popen\s*\(",
    r"\bsubprocess\.(call|run|Popen|check_output|check_call)\s*\(",
    r"\bcommands\.(getoutput|getstatusoutput)\s*\(",
    # Dateisystem destruktiv
    r"\bshutil\.rmtree\s*\(",
    r"\bshutil\.move\s*\(",
    r"\bos\.remove\s*\(",
    r"\bos\.unlink\s*\(",
    r"\bos\.rmdir\s*\(",
    r"\bos\.renames\s*\(",
    r"\bopen\s*\(\s*['\"]\s*(?:/|[A-Za-z]:\\|~|\.\.)",
    r"\bwinreg\.(CreateKey|DeleteKey|SetValue)\s*\(",
    r"\bctypes\.(windll|cdll)\b",
    r"\bsocket\.(connect|sendto)\s*\(",
    r"\brequests\.(post|put|patch|delete)\s*\(",
    r"\bhttpx\.(post|put|patch|delete)\s*\(",
    # Code-Ausfuehrung
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bcompile\s*\(",
    r"\b__import__\s*\(",
]


def _is_destructive_python(code: str) -> str | None:
    """Return matching pattern if Python code contains destructive patterns."""
    import re as _dre
    _code = str(code)
    for _pat in _DESTRUCTIVE_PYTHON_PATTERNS:
        if _dre.search(_pat, _code):
            return _pat
    return None


async def _destructive_gate(name: str, args: dict) -> str | None:

    _destructive = False
    _reason = ""
    _details = ""

    if name == "run_python":
        _code = str(args.get("code", "") or args.get("source", ""))
        _matched = _is_destructive_python(_code)
        if _matched:
            _destructive = True
            _reason = f"run_python-Code matcht destruktives Muster: '{_matched}'"
            _details = _code[:200]
    elif name == "run_bash":
        _cmd = str(args.get("cmd", "") or args.get("command", ""))
        _matched = _is_destructive_bash(_cmd)
        if _matched:
            _destructive = True
            _reason = f"run_bash-Kommando matcht destruktives Muster: '{_matched}'"
            _details = _cmd[:200]

    if not _destructive:
        return None

    run_id = _current_run_id.get()
    if not run_id:
        import logging as _dgl
        _note_tool_use("", "destructive_gate", "no_run_id")
        _dgl.getLogger("hivemind.tools").warning(
            "[GATE] Destructive action without run_id: %s — %s | cmd=%s",
            name, _reason, _details,
        )
        return _tool_error_response(
            "GATE_NO_RUN_ID",
            f"[DESTRUCTIVE GATE] {_reason}. No run_id — action denied.",
            tool=name, details={"reason": _reason, "cmd": _details},
        )

    from infra import run_control
    _emit = _tool_loop_emit.get(None)
    _question = (
        f"[DESTRUCTIVE GATE] {name} requires confirmation:\n"
        f"  {_details}\n\n"
        f"  Reason: {_reason}\n\n"
        f"  Reply with 'yes' to confirm or 'no' to decline."
    )

    if _emit:
        try:
            await _emit({"type": "agent_asking", "question": _question, "run_id": run_id})
            await _emit({"type": "status", "content": "Destructive action requires confirmation..."})
        except Exception:
            pass

    _timeout = _pause_timeout_s.get()
    await run_control.initiate_pause(run_id, _question)
    _answer = await run_control.wait_for_resume(run_id, timeout_s=_timeout)

    if _emit:
        try:
            await _emit({"type": "agent_resumed"})
        except Exception:
            pass

    _answer_lower = _answer.strip().lower() if isinstance(_answer, str) else ""
    if _answer_lower in ("ja", "yes", "y", "ok", "ausfuehren", "execute", "go", "confirm"):
        import logging as _dgl3
        _dgl3.getLogger("hivemind.tools").info(
            "[GATE] Destructive action CONFIRMED: %s — %s | cmd=%s",
            name, _reason, _details,
        )
        _note_tool_use(run_id, "destructive_gate", "confirmed")
        return None  # confirmed → execute

    # Internal classification (log/level only) — the returned model text
    # stays byte-identical to the existing template in all deny cases.
    import logging as _dgl_gate
    _reason_kind = "declined"
    _log_level = _dgl_gate.INFO
    if not isinstance(_answer, str) or not _answer.strip():
        _reason_kind = "empty_answer"
        _log_level = _dgl_gate.WARNING
    elif _answer_lower.startswith("[ask_user timeout"):
        _reason_kind = "timeout"
        _log_level = _dgl_gate.WARNING
    elif _answer_lower.startswith("[ask_user error"):
        _reason_kind = "no_pause_event"
        _log_level = _dgl_gate.WARNING

    if _log_level >= _dgl_gate.WARNING:
        _dgl_gate.getLogger("hivemind.tools").log(
            _log_level,
            "[GATE] Destructive action NOT EXECUTED (%s): %s — cmd=%s",
            _reason_kind, name, _details,
        )
    else:
        _dgl_gate.getLogger("hivemind.tools").info(
            "[GATE] Destructive action DECLINED: %s — answer=%s | cmd=%s",
            name, _answer, _details,
        )
    _note_tool_use(run_id, "destructive_gate", _reason_kind)
    return (
        f"[DESTRUCTIVE GATE: DECLINED] The action was declined by the user: "
        f"'{_answer}'. The command was NOT executed. "
        f"Try a less destructive alternative."
    )


async def _run_inline_tool(
    name: str,
    args: dict,
    workspace_lock: str | None = None,
    *,
    tool_mode: str | None = None,
    include_websearch: bool = False) -> str:
    """Primary inline-tool dispatch using the unified handler map."""
    workspace = Path(workspace_lock) if workspace_lock else Path(os.environ.get("HIVEMIND_WORKSPACE", "."))

    _read_set = _files_read_in_run.get(None)
    if _read_set is None:
        _read_set = set()
        _files_read_in_run.set(_read_set)

    # ANY-READ-SET (2026-08-19): getrennt vom SKIP-Set — siehe Deklaration oben.
    _any_read_set = _files_seen_in_run.get(None)
    if _any_read_set is None:
        _any_read_set = set()
        _files_seen_in_run.set(_any_read_set)

    # WRITTEN-SET (2026-08-21): Session-eigene Schreibziele — siehe Deklaration.
    _written_set = _files_written_in_run.get(None)
    if _written_set is None:
        _written_set = set()
        _files_written_in_run.set(_written_set)

    _resolve_hint = ""     # fuzzy path resolution hint for edit tools

    if name == "read_file" and "path" in args:
        _rp_str = _normalize_tool_path(args["path"], workspace)
        _any_read_set.add(_rp_str)
        _ctx_set = _files_in_context.get(None)
        if _ctx_set and _rp_str in _ctx_set:
            import logging as _logging
            _logging.getLogger("hivemind.tools").debug(
                "[HINT-LOG] file=%s in_pre_explore_context=True read_allowed=True",
                _rp_str
            )
        if _rp_str in _read_set and not args.get("start_line") and not args.get("end_line"):
            return _normalize_meta_paths(
                f"[SKIP: '{args.get('path')}' already read in this session. "
                f"The full content is in your context above. "
                f"Do NOT call read_file on this path again. "
                f"Use edit_file or write_file directly.]"
            )
        if not args.get("start_line") and not args.get("end_line"):
            _read_set.add(_rp_str)

    elif name in ("edit_file", "patch_file", "write_file", "write_file_append", "replace_lines") and "path" in args:
        raw_path = args["path"]
        _resolved_path = raw_path
        _fp_check = Path(_resolved_path)
        if not _fp_check.is_absolute():
            _fp_check = workspace / _fp_check
        try:
            _fp_check = _fp_check.resolve()
        except Exception:
            pass
        if _resolved_path != raw_path:
            _resolve_hint = f"[Path resolved: {raw_path} → {_resolved_path}]\n"
            args["path"] = _resolved_path
        else:
            _resolve_hint = ""
        _target = _normalize_tool_path(str(_fp_check), workspace)
        _ctx_set = _files_in_context.get(None)
        _in_context = bool(_ctx_set and _target in _ctx_set)
        # WRITE-GUARD-RELAX (2026-09-02): the read-before-write guard stops blind
        # overwrites in the direct chat. The agentic/duo coder (duo_full) is an
        # explicit full-access coding agent working in the workspace — blocking
        # write_file/edit_file on existing files there produced red errors and
        # empty diffs ("Waiting for coder output.").
        _guard_active = (not _is_external_dispatch() and _read_guard_enabled()
                         and (tool_mode or "") != "duo_full")
        if _guard_active and _fp_check.exists() and _target not in _read_set and _target not in _any_read_set and _target not in _written_set and not _in_context:
            if name == "write_file":
                return _normalize_meta_paths(
                    f"[TOOL_ERROR: READ_REQUIRED]\n"
                    f"Action: write_file BLOCKED on '{raw_path}'\n"
                    f"Reason: File has not been read in this session.\n"
                    f"Fix: Call read_file('{raw_path}') first, then use edit_file to modify it.\n"
                    f"Note: This file already exists. write_file is only for creating "
                    f"NEW files — use edit_file (SEARCH/REPLACE) to modify it instead."
                )
            return _normalize_meta_paths(
                f"[TOOL_ERROR: READ_REQUIRED]\n"
                f"Action: {name} BLOCKED on '{raw_path}'\n"
                f"Reason: File has not been read in this session.\n"
                f"Fix: Call read_file('{raw_path}') first, then retry {name}."
            )
    if tool_mode:
        _allowed = _tool_names_for_mode(tool_mode, include_websearch=include_websearch)
        if name not in _allowed:
            return _tool_error_response(
                "TOOL_NOT_ALLOWED",
                f"Tool '{name}' is not allowed in mode '{tool_mode}'.",
                tool=name,
                mode=tool_mode ,
                details={"allowed_tools": sorted(_allowed)})
    _path_based_tools = {"read_file", "get_signatures", "patch_file", "edit_file", "write_file", "replace_lines", "search_code"}
    _WRITE_TOOLS_NO_FUZZY = {"patch_file", "edit_file", "write_file", "write_file_append", "replace_lines"}
    if name in _path_based_tools and "path" in args:
        requested = args["path"]
        if not Path(requested).exists():
            if name not in _WRITE_TOOLS_NO_FUZZY:
                corrected = _fuzzy_resolve_path(requested, str(workspace))
                if corrected:
                    args["path"] = corrected
                elif name == "read_file":
                    if not requested.endswith(('.py', '.ts', '.js', '.jsx', '.tsx', '.json', '.yaml', '.yml', '.toml')):
                        _with_ext = requested.rsplit('.', 1)[0] if '.' in Path(requested).name else requested + '.py'
                        corrected_ext = _fuzzy_resolve_path(_with_ext, str(workspace))
                        if corrected_ext:
                            args["path"] = corrected_ext
    handler = _INLINE_TOOL_HANDLER_MAP.get(name)
    if handler is None:
        pass
    if handler is not None:
        # ── A-P1-9: Web-Search-Call-Budget (Governor analog ask_user) ──
        if name in ("web_search", "web_fetch") and _consume_web_search_budget():
            return _tool_error_response(
                "WEBSEARCH_BUDGET_EXHAUSTED",
                "Web search budget exhausted (duo_websearch_max_calls calls per run). "
                "Do not keep searching — work with the context you already have "
                "or document the open point in task_complete.",
                tool=name,
                mode=str(tool_mode or "") )

        # ── A-P1-6: Dependency-Install-Call-Budget ──
        if name == "install_package" and _consume_install_budget():
            return _tool_error_response(
                "INSTALL_BUDGET_EXHAUSTED",
                "Dependency install budget exhausted (duo_install_max_calls calls/run). "
                "No further installs — work with the existing "
                "dependencies or document the missing dependency in task_complete.",
                tool=name,
                mode=str(tool_mode or "") )

        if name in ("run_bash", "run_python"):
            _gate_block = await _destructive_gate(name, args)
            if _gate_block is not None:
                return _gate_block

        _tok = _dispatch_active_cv.set(True)
        try:
            _result = await handler(args or {}, workspace, workspace_lock)
        finally:
            _dispatch_active_cv.reset(_tok)

        if name == "read_file" and not args.get("start_line") and not args.get("end_line"):
            if "[FILE TRUNCATED" in _result or "[TRUNCATED:" in _result:
                _rp_trunc = _normalize_tool_path(args["path"], workspace)
                _read_set.discard(_rp_trunc)

        # WRITTEN-SET (2026-08-21): erfolgreiche write_file/write_file_append
        if name in ("write_file", "write_file_append"):
            if not _result.startswith("[TOOL_ERROR") and args.get("path"):
                _w_raw = args["path"]
                _w_res = _fuzzy_resolve_path(_w_raw, str(workspace)) or _w_raw
                _w_fp = Path(_w_res)
                if not _w_fp.is_absolute():
                    _w_fp = workspace / _w_fp
                try:
                    _w_fp = _w_fp.resolve()
                except Exception:
                    pass
                _written_set.add(_normalize_tool_path(str(_w_fp), workspace))

        if name in ("write_file", "edit_file", "patch_file", "replace_lines", "write_file_append"):
            if not _result.startswith("[TOOL_ERROR") and args.get("path"):
                try:
                    _diff = get_transaction().diff_for(args["path"])
                    if _diff:
                        _result += "\n\n[Diff — what changed]\n```diff\n" + _diff + "\n```"
                except Exception:
                    pass

        # ── ProjectState: Build-Steps aufzeichnen ──
        _ps = _current_project_state.get()
        if _ps is not None and name in ("write_file", "edit_file", "run_bash", "run_tests", "task_complete"):
            _path = args.get("path", "")
            _is_success = not _result.startswith("[TOOL_ERROR")
            if name in ("write_file", "edit_file"):
                _action = "create" if name == "write_file" else "edit"
                _ps.add_build_step(_action, _path, _is_success,
                                   f"{'Erstellt' if _action == 'create' else 'Bearbeitet'}: {Path(_path).name}")
            elif name == "run_bash":
                _cmd = args.get("cmd", "") or args.get("command", "")
                _action = "test" if any(kw in str(_cmd).lower() for kw in ("test", "pytest", "npm test", "cargo test", "go test")) else "verify"
                if _is_success:
                    from utils.tool import run_bash_failed as _rb_failed
                    _is_success = not _rb_failed(_result)
                _ps.add_build_step(_action, None, _is_success,
                                   f"{_action}: {str(_cmd)[:80]}")
            elif name == "task_complete":
                _summary = str(args.get("summary", "") or args.get("result", ""))[:100]
                _ps.add_build_step("complete", None, _is_success,
                                   _summary or "Task completed")

        if _resolve_hint:
            _result = _resolve_hint + _result
        return _normalize_meta_paths(_result)
    return _tool_error_response(
        "TOOL_NOT_FOUND",
        f"Unknown inline tool '{name}'.",
        tool=name,
        mode=str(tool_mode or "") )


