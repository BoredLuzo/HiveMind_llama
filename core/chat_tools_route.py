"""Inline tool-agent route (C7, 2026-11-07 maintainability sprint).

The `_route_as_tool` branch of run_stream, moved VERBATIM behind an
async-generator seam. It starts the short tool loop, mirrors its events
and ends the run (return-terminated -> ps.stop).
"""
from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable

import httpx

from core import state as _state
from core.state import _get_num_ctx, registry_get, settings
from context.chat_util import _make_messages
from infra.run_control import _unregister_abort, _unregister_step_skip
from tools.definitions import _get_inline_tools
from utils.httpx_utils import make_httpx_timeout as _make_httpx_timeout

logger = logging.getLogger("hivemind.chat_run")

# A4 (2026-11-07): the legacy tool route had its own "just act" prompt and 6
# rounds; a read-only task then kept exploring and ended at max_tool_rounds.
# Share the direct-tools advertisement (one source for the tool rules) and add
# an explicit stop rule. Direct route uses the same _direct_tools_note.
_TOOL_STOP_RULE = (
    "\n\nSTOP RULE: when you have the answer or the task is done, reply with "
    "the final answer in plain text and do NOT call another tool. Only call a "
    "tool while it is still needed."
)


def _tool_system_prompt(ws_ok: bool) -> str:
    """Tool advertisement shared with the direct route + the stop rule."""
    try:
        from core.direct_runner import _direct_tools_note
        _note = _direct_tools_note("direct_full", ws_ok=ws_ok)
    except (ImportError, AttributeError):
        _note = "\n\nTOOLS ARE AVAILABLE."
    return _note + _TOOL_STOP_RULE


def _tool_signatures(msg: dict) -> list[str]:
    """Stable signatures for one assistant message's tool calls."""
    out: list[str] = []
    for _tc in (msg or {}).get("tool_calls") or []:
        _fn = (_tc or {}).get("function") or {}
        _name = str(_fn.get("name") or "")
        _args = _fn.get("arguments", "")
        if not isinstance(_args, str):
            import json as _json
            try:
                _args = _json.dumps(_args, sort_keys=True, ensure_ascii=False)
            except (TypeError, ValueError):
                _args = str(_args)
        if _name:
            out.append(f"{_name}:{_args}")
    return out


def _repeat_guard():
    """on_after_round callback: nudge an identical repeated tool call once."""
    _counts: dict[str, int] = {}
    _nudged: set[str] = set()

    async def _after_round(msg, msgs, _round, _state):
        for _sig in _tool_signatures(msg):
            _counts[_sig] = _counts.get(_sig, 0) + 1
            if _counts[_sig] >= 2 and _sig not in _nudged:
                _nudged.add(_sig)
                msgs.append({"role": "user", "content": (
                    "You already called this tool with the same arguments. "
                    "The result is above - do not repeat it. Answer the user "
                    "now, or use a different tool/arguments if something is "
                    "still missing.")})
        return True

    return _after_round


def _tool_round_limit(cfg: dict) -> int:
    """direct_tools_max_rounds clamped to 1..300 (same as direct_runner)."""
    _raw = (cfg or {}).get("direct_tools_max_rounds", 12)
    if _raw is None or _raw == "":
        _raw = 12
    try:
        _raw = int(_raw)
    except (TypeError, ValueError):
        _raw = 12
    return max(1, min(300, _raw))


@dataclasses.dataclass
class _ChatToolsRouteState:
    """Inputs of the tool route; `stop` ends the run."""
    judge_toolsize: str = ""
    models_cache: list = dataclasses.field(default_factory=list)
    user_input: str = ""
    effective_images: list = dataclasses.field(default_factory=list)
    ws_str: str = ""
    run_id: str = ""
    t_total: float = 0.0
    websearch_available: bool = False
    aborted: Callable = lambda: False
    emit: Callable = lambda *a, **kw: None      # async emit(event) -> str
    done_event: Callable = lambda *a, **kw: {}
    collect_done_metrics: Callable = lambda: {}
    stop: bool = False


async def _chat_tools_route(ps: _ChatToolsRouteState):
    _judge_toolsize = ps.judge_toolsize
    _available = list(ps.models_cache) if ps.models_cache else []
    _tool_websearch = ps.websearch_available
    user_input = ps.user_input
    effective_images = ps.effective_images
    _ws_str = ps.ws_str
    run_id = ps.run_id
    t_total = ps.t_total
    _aborted = ps.aborted
    emit = ps.emit
    _done_event = ps.done_event
    _collect_done_metrics = ps.collect_done_metrics

    _available = list(ps.models_cache) if ps.models_cache else []
    # small: simple file reads, git - qwen2.5:3b suffices
    if _judge_toolsize == "large":
        _tool_priority = ["qwen3.5:4b", "qwen3.5:9b-ud", "qwen3.5:2b", "granite-4.1:3b"]
    else:
        _tool_priority = ["qwen3.5:2b", "granite-4.1:3b", "qwen3.5:4b", "qwen3.5:9b-ud"]
    _tool_model = next((m for m in _tool_priority if m in _available),
                       registry_get("direct"))
    _tool_ctx_override = max(_get_num_ctx(_tool_model) or 4096, 8192)
    _tool_opts: dict = {"temperature": 0.1, "num_predict": 1200, "num_ctx": _tool_ctx_override}
    _tool_ws = bool(settings.get("duo_websearch_enabled", False)) and _tool_websearch
    _tool_sys = (
        "Use the available tools to complete file and code operations. Be direct and concise.\n\n"
        "Available tools:\n"
        "  read_file(path, start_line?, end_line?) - read file (optional: line range only)\n"
        "  get_signatures(path, max_items?) - structural overview with line numbers\n"
        "  find_files(pattern, path?)  - glob search: '**/*.py', 'src/*.ts'\n"
        "  list_dir(path)              - list directory contents\n"
        "  search_code(pattern, path?) - regex search across code files\n"
        "  edit_file(path, old_text, new_text) - surgical edit (preferred for modifications)\n"
        "  edit_file(path, edits)      - create new files OR modify existing ones\n"
        "  run_bash(cmd)               - shell command: tests, pip install, npm run, grep...\n"
        "  run_python(code)            - execute python snippet directly\n"
        "  git_status(cmd)             - git: status|diff|log|show\n"
        "  git_commit(message)         - commit all changes with message\n\n"
        "Workflow:\n"
        "  1. Explore first (list_dir / find_files / read_file)\n"
        "  2. Modify existing files with edit_file\n"
        "  3. After implementing, run tests (run_bash pytest / npm test)\n"
        "  4. On failure: re-read the region and edit_file again\n"
        "  Do not ask for confirmation - just act."
    ) + _tool_system_prompt(_tool_ws)
    _tool_msgs = _make_messages(_state.pipeline, _tool_sys, user_input, effective_images, False, False)
    yield await emit({"type": "complexity", "content": "simple", "source": "tool"})
    yield await emit({"type": "agent", "content": "Tool Agent", "model": _tool_model})
    _tool_t = time.time()
    _tool_loop_msgs = list(_tool_msgs)
    _tool_out_parts: list[str] = []
    # A4 (2026-11-07): round-limit parity with the direct route - one setting
    # controls both, so a user does not get 6 rounds on this path and 12 on
    # the direct one.
    _tool_max_rounds = _tool_round_limit(settings)
    _tool_calls: bool = False
    _tool_round = 0
    _tool_stop_reason = "completed"
    # SCHEMA-FIT (2026-10-17): this path shipped the full 25-tool schema
    # (~15.6k chars) whatever the tool-agent ctx was. Fit it to the window.
    _tool_schema_budget = 0
    try:
        from core.budgets import schema_budget_for as _sbf
        _tool_schema_budget = _sbf(_tool_ctx_override, _tool_model)
    except (ImportError, AttributeError, TypeError, ValueError):
        _tool_schema_budget = 0
    _active_tools = _get_inline_tools(include_websearch=_tool_ws, mode="tool_agent",
                                      budget_chars=_tool_schema_budget or None)
    # Timeout strategy:
    _tool_read_timeout_s = float(settings.get("duo_llm_slow_timeout_s", 300))
    _tool_http_timeout = _make_httpx_timeout(read_s=_tool_read_timeout_s)
    try:
        from backend.llama_server_manager import manager as _lsm
        _tool_port = await _lsm.ensure_loaded(_tool_model, num_ctx=_tool_opts.get("num_ctx", 4096))
        async with httpx.AsyncClient(timeout=_tool_http_timeout) as _tc:
            from core.tool_loop import ToolLoop, ToolLoopConfig
            _loop = ToolLoop(
                config=ToolLoopConfig(
                    stream=False, max_rounds=_tool_max_rounds, max_post_attempts=1,
                    model=_tool_model, temperature=_tool_opts.get("temperature", 0.2),
                    max_tokens=_tool_opts.get("num_predict", 1200),
                    num_ctx=_tool_opts.get("num_ctx", 4096),
                    tools=_active_tools, tool_mode="tool_agent",
                    include_websearch=_tool_ws,
                    read_timeout_s=_tool_read_timeout_s,
                ),
                http_client=_tc, port=_tool_port,
                workspace=_ws_str,
                abort_check=_aborted,
                on_after_round=_repeat_guard(),
            )
            async for _ev in _loop.run(_tool_loop_msgs):
                if _ev["type"] == "token":
                    _tool_out_parts.append(_ev["content"])
                yield await emit(_ev)
            _tool_stop_reason = _loop.state.stop_reason
            _tool_calls = bool(_loop.state.tool_calls_made)
    except httpx.TimeoutException as _te:
        _elapsed_so_far = round(time.time() - _tool_t, 1)
        _err = f"[Tool timeout after {_elapsed_so_far}s - model did not respond in time. try a smaller model or simplify the request.]"
        yield await emit({"type": "token", "content": _err})
        _tool_out_parts.append(_err)
        _tool_stop_reason = "timeout"
    except Exception as _te:
        _err = f"[Tool error: {type(_te).__name__}: {str(_te)[:200]}]"
        yield await emit({"type": "token", "content": _err})
        _tool_out_parts.append(_err)
        _tool_stop_reason = "error"
    else:
        if _tool_stop_reason == "max_tool_rounds" and _tool_calls and not _aborted():
            _limit_msg = f"\n[Max. {_tool_max_rounds} tool rounds reached]"
            yield await emit({"type": "token", "content": _limit_msg})
            _tool_out_parts.append(_limit_msg)
        elif _aborted():
            _tool_stop_reason = "aborted"
    _tool_out = "".join(_tool_out_parts)
    yield await emit({"type": "agent_done", "elapsed": round(time.time() - _tool_t, 1)})
    _state.memory.add_to_session("user", user_input)
    _state.memory.add_to_session("assistant", _tool_out)
    yield await emit(_done_event(round(time.time() - t_total, 1), _tool_stop_reason, **_collect_done_metrics()))
    _unregister_abort(run_id)
    _unregister_step_skip(run_id)
    ps.stop = True
    return
