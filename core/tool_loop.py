# -*- coding: utf-8 -*-
"""Unified tool-loop base class for all agent modes."""
from __future__ import annotations
import asyncio
import json
import json as _json_tc
import logging
import re as _re_tc
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable, Awaitable

import httpx

from tools.errors import tool_call_failed, tool_error_has_code
from tools.definitions import _get_inline_tools, _filter_tools_for_mode, get_tools_for_phase
from tools.runner import _run_inline_tool, _current_run_id, _pause_timeout_s, _tool_loop_emit
from sse.events import make_tool_call_event as _make_tool_call_event, make_tool_result_event as _make_tool_result_event
from core.duo_helpers import RE_THINK_CLEANUP as _RE_THINK_CLEANUP, parse_sse_delta as _parse_sse_delta
from core.model_sampling import get_sampling_profile
from utils.tool import parse_tool_args as _parse_tool_args, run_bash_failed as _run_bash_failed

# ── K1: Multi-Format Tool-Call Fallback Extraction ─────────────────────

_TC_FALLBACK_PATTERNS: list = [
    _re_tc.compile(r'```(?:json)?\s*(\{[^`]+\})\s*```', _re_tc.DOTALL),
    _re_tc.compile(r'(\{\s*"name"\s*:\s*"[a-z_]+"[^}]*\})', _re_tc.DOTALL),
    _re_tc.compile(r'"tool_calls"\s*:\s*(\[[^\]]+\])', _re_tc.DOTALL),
]


def _fallback_extract_tool_calls(content: str) -> list[dict]:
    for _pat in _TC_FALLBACK_PATTERNS:
        for _match in _pat.finditer(content or ""):
            _raw = _match.group(1)
            try:
                _parsed = _json_tc.loads(_raw)
            except (_json_tc.JSONDecodeError, ValueError):
                continue
            if isinstance(_parsed, dict) and "name" in _parsed and isinstance(_parsed.get("arguments"), dict):
                _name = _parsed["name"]
                _args_str = _json_tc.dumps(_parsed["arguments"])
                return [{"id": f"fb_{_name}", "type": "function", "function": {"name": _name, "arguments": _args_str}}]
            if isinstance(_parsed, list):
                _valid = []
                for tc in _parsed:
                    if not isinstance(tc, dict):
                        continue
                    if "function" in tc and isinstance(tc["function"], dict):
                        _fn = tc["function"]
                        _name = _fn.get("name", "")
                        _args_raw = _fn.get("arguments", {})
                    else:
                        _name = tc.get("name", "")
                        _args_raw = tc.get("arguments", {})
                    if not _name:
                        continue
                    _args_str = _json_tc.dumps(_args_raw) if isinstance(_args_raw, dict) else str(_args_raw or "{}")
                    _valid.append({"id": f"fb_{_name}", "type": "function", "function": {"name": _name, "arguments": _args_str}})
                if _valid:
                    return _valid
                continue
            continue
    return []


# ── K2: Retry Feedback Injection ──────────────────────────────────────

def _inject_retry_feedback(
    messages: list,
    error_type: str,
    bad_content: str = "",
    tool_names: list | None = None,
) -> None:
    if bad_content:
        messages.append({"role": "assistant", "content": bad_content[:200]})
    if error_type == "parse_error":
        _fb = "Your last response contained invalid JSON in the tool call. Respond with ONLY a valid JSON tool call. No markdown, no text."
    elif error_type == "no_tool_call":
        _fb = "Your last response contained no tool call. You MUST respond with a tool call. No text, no explanation."
    elif error_type == "unknown_tool":
        _names_json = json.dumps((tool_names or [])[:8])
        _fb = f"Your last response used an unknown tool name. Valid tool names: {_names_json}. Use exactly one of these names."
    else:
        _fb = "Your last response was invalid. Respond with a valid tool call."
    messages.append({"role": "user", "content": _fb})


# ── L1: GBNF Grammar Feature-Flag + Generator ─────────────────────────

_USE_GBNF_GRAMMAR: bool = True


def _build_tool_call_grammar(tool_names: list[str]) -> str | None:
    if not tool_names:
        return None
    _names_grammar = " | ".join(f'"\\"{n}\\""' for n in tool_names)
    _base = """\
ws ::= [ \\t\\n]*
string ::= "\\"" ([^"\\\\] | "\\\\" .)* "\\""
number ::= "-"? [0-9]+ ("." [0-9]+)?
boolean ::= "true" | "false"
null ::= "null"
value ::= string | number | boolean | null | array | object
array ::= "[" ws (value (ws "," ws value)*)? ws "]"
pair ::= string ws ":" ws value
object ::= "{" ws (pair (ws "," ws pair)*)? ws "}"
root ::= ws tool-call ws
tool-call ::= "{{" ws "\\"name\\"" ws ":" ws tool-name ws "," ws "\\"arguments\\"" ws ":" ws object ws "}}"
tool-name ::= """ + _names_grammar
    return _base


def _grammar_compatible(payload: dict) -> bool:


    if not isinstance(payload, dict):
        return True
    return not (payload.get("tools") or payload.get("tool_choice"))


@dataclass
class ToolLoopConfig:
    """All configuration for a ToolLoop run. All fields have sensible defaults."""
    stream: bool = False
    max_rounds: int = 6
    max_post_attempts: int = 1
    read_timeout_s: float = 300.0

    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 1200
    thinking: bool = False
    thinking_budget: int = 0
    num_ctx: int = 4096

    tools: list[dict] = field(default_factory=list)
    tool_mode: str = ""
    tool_phase: str = "all"
    include_websearch: bool = False

    compress_enabled: bool = False
    loop_detect: bool = False
    verify_guard: bool = False
    # When True (default), a round without a tool call triggers K2 "no_tool_call"
    # feedback + retry (tool-agent behaviour). Set False for chat modes where a
    # direct answer without tools is a valid final answer (direct chat tools).
    require_tool_call: bool = True

    retry_on_5xx: bool = True
    retry_on_404: bool = False
    retry_on_connect: bool = False
    retry_on_read_timeout: bool = False
    retry_on_grammar_crash: bool = False
    retry_on_parse_error: bool = False
    retry_on_context_overflow: bool = False


@dataclass
class ToolLoopState:
    """Mutable state tracked across tool rounds."""
    rounds_used: int = 0
    tool_calls_made: int = 0
    tool_errors: int = 0
    content_parts: list[str] = field(default_factory=list)
    file_changes: dict = field(default_factory=dict)
    stop_reason: str = "completed"

    verify_mutation_serial: int = 0
    verify_last_ok_serial: int = 0
    verify_guard_hits: int = 0

    parse_errors: int = 0
    fail_streak: int = 0
    think_only_retries: int = 0
    think_runtime: bool = False

    call_sigs: list[int] = field(default_factory=list)

    last_run_bash_failure: dict | None = None
    changed_since_failure: set = field(default_factory=set)


# ── Callbacks ──────────────────────────────────────────────────────────

OnToolResult = Callable[[str, dict, str, "ToolLoopState"], Awaitable[str | None]]
"""Called after tool execution: (tool_name, args, result, state) -> optional modified result."""

OnBeforePost = Callable[[list[dict], int, "ToolLoopState"], Awaitable[tuple[list[dict], dict | None]]]
"""Called before each POST: (messages, round_num, state) -> (modified_messages, payload_overrides or None)."""

OnAfterRound = Callable[[dict, list[dict], int, "ToolLoopState"], Awaitable[bool]]
"""Called after a round completes: (msg, messages, round_num, state) -> continue_looping."""

OnCompress = Callable[[list[dict], int], Awaitable[list[dict]]]
"""Called to compress messages: (messages, round_num) -> compressed_messages."""


# ── ToolLoop ────────────────────────────────────────────────────────────

class ToolLoop:
    """Unified tool-calling loop for duo_runner, tool_agent, and openai_agent."""

    def __init__(
        self,
        config: ToolLoopConfig,
        http_client: httpx.AsyncClient,
        port: int,
        *,
        workspace: str = "",
        run_id: str = "",
        on_tool_result: OnToolResult | None = None,
        on_before_post: OnBeforePost | None = None,
        on_after_round: OnAfterRound | None = None,
        on_compress: OnCompress | None = None,
        abort_check: Callable[[], bool] | None = None,
        custom_executor: Callable[[str, dict], Awaitable[str]] | None = None,
        emit_status=None,
    ):
        self.cfg = config
        self._client = http_client
        self._port = port
        self._workspace = workspace
        self._run_id = run_id
        self._on_tool_result = on_tool_result
        self._on_before_post = on_before_post
        self._on_after_round = on_after_round
        self._on_compress = on_compress
        self._abort_check = abort_check
        self._custom_executor = custom_executor
        self._emit_status = emit_status

    async def run(self, messages: list[dict]) -> AsyncIterator[dict]:
        """Run the tool-calling loop, yielding events."""
        state = ToolLoopState()
        if self._run_id:
            _current_run_id.set(self._run_id)

        _tool_messages = list(messages)
        _tool_names_for_guard = {"patch_file", "edit_file", "write_file", "write_file_append", "replace_lines"}
        _MAX_FEEDBACK_ROUNDS = 3
        _feedback_injected_count = 0

        for _round in range(self.cfg.max_rounds):
            if self._abort_check and self._abort_check():
                state.stop_reason = "aborted"
                break
            state.rounds_used = _round + 1

            # ── before_post callback ──
            if self._on_before_post:
                _tool_messages, _overrides = await self._on_before_post(_tool_messages, _round, state)
            else:
                _overrides = None

            # ── Build payload ──
            _tools_payload = self.cfg.tools
            if self.cfg.tool_phase and self.cfg.tool_phase != "all":
                _phase_tools = get_tools_for_phase(self.cfg.tool_phase)
                if _phase_tools:
                    _tools_payload = _phase_tools
            # N1: Model-aware sampling profile
            _smp = get_sampling_profile(self.cfg.model, thinking=bool(self.cfg.thinking))
            _payload = {
                "model": self.cfg.model,
                "messages": _tool_messages,
                "tools": _tools_payload,
                "stream": self.cfg.stream,
                "temperature":       _smp.get("temperature", self.cfg.temperature),
                "top_p":             _smp.get("top_p", 0.95),
                "top_k":             int(_smp.get("top_k", 20)),
                "min_p":             float(_smp.get("min_p", 0.0)),
                "presence_penalty":  float(_smp.get("presence_penalty", 0.0)),
                "repeat_penalty":    float(_smp.get("repetition_penalty", 1.0)),
                "max_tokens": self.cfg.max_tokens,
            }
            if self.cfg.thinking:
                _payload["thinking"] = True
                _payload["thinking_budget"] = self.cfg.thinking_budget
            if _overrides:
                _payload.update(_overrides)

            # ── Compress if enabled ──
            if self.cfg.compress_enabled and self._on_compress:
                _tool_messages = await self._on_compress(_tool_messages, _round)
                _payload["messages"] = _tool_messages

            # ── POST + parse with retry ──
            _msg = _content_text = None
            _tool_calls = []
            if self.cfg.stream:
                async for _ev in self._post_stream_with_retry(_payload):
                    if isinstance(_ev, tuple):
                        _msg, _content_text, _tool_calls = _ev
                    else:
                        yield _ev
            else:
                _msg, _content_text, _tool_calls, _events = await self._post_with_retry(_payload)
                for _ev in _events:
                    yield _ev

            if not _msg:
                continue

            # ── K1: Multi-Format Fallback Extraction ──
            if not _tool_calls and _content_text:
                _fallback_tcs = _fallback_extract_tool_calls(_content_text)
                if _fallback_tcs:
                    logging.getLogger(__name__).debug(
                        "[TC_FALLBACK] Extracted %d tool call(s) from content text", len(_fallback_tcs)
                    )
                    _tool_calls = _fallback_tcs
                    _msg["tool_calls"] = _tool_calls

            # ── No tool calls → K2 feedback or done ──
            if not _tool_calls:
                _content = (_msg.get("content") or "").strip()
                # think-strip fallback
                if _content and "<think" in _content:
                    _content = _RE_THINK_CLEANUP.sub("", _content).strip()

                # K2: inject no_tool_call feedback and retry — only when a tool
                # call is required (tool-agent / openai_agent / coder). Chat
                # modes with require_tool_call=False accept the direct answer.
                if self.cfg.require_tool_call and _feedback_injected_count < _MAX_FEEDBACK_ROUNDS:
                    _inject_retry_feedback(
                        _tool_messages,
                        error_type="no_tool_call",
                        bad_content=_content_text or _content or "",
                    )
                    _feedback_injected_count += 1
                    state.fail_streak += 1
                    continue

                if _content:
                    state.content_parts.append(_content)
                    yield {"type": "token", "content": _content}
                if self.cfg.verify_guard and state.verify_mutation_serial > state.verify_last_ok_serial:
                    state.verify_guard_hits += 1
                    if state.verify_guard_hits >= 2:
                        state.stop_reason = "verification_required_after_write"
                        yield {"type": "token", "content": "Verification required: run_bash must pass after file changes."}
                        break
                    _tool_messages.append(_msg)
                    _tool_messages.append({"role": "user", "content": "Before finalizing, run verification now. Call run_bash with a project test/build command and ensure exit code 0. Then give the final answer."})
                    continue
                state.stop_reason = "completed"
                break

            # ── Execute tool calls ──
            _tool_messages.append(_msg)
            _known_tool_names = {t.get("function", {}).get("name", "") for t in self.cfg.tools}
            _unknown_tool_abort = False
            for _tc in _tool_calls:
                _fn = _tc.get("function", {})
                _name = _fn.get("name", "")

                # K2: unknown tool name → feedback + retry
                if _name and _name not in _known_tool_names:
                    if _feedback_injected_count < _MAX_FEEDBACK_ROUNDS:
                        _inject_retry_feedback(
                            _tool_messages,
                            error_type="unknown_tool",
                            bad_content=_name,
                            tool_names=sorted(_known_tool_names),
                        )
                        _feedback_injected_count += 1
                    state.tool_errors += 1
                    _unknown_tool_abort = True
                    break

                _args = _parse_tool_args(_fn.get("arguments", {}))

                state.tool_calls_made += 1

                yield await self._emit(_make_tool_call_event(_name, _args))

                # Set ask_user event emitter for tool_agent/openai_agent modes
                _ask_events: list[dict] = []
                async def _capture_ask_event(ev):
                    _ask_events.append(ev)
                _tool_loop_emit.set(_capture_ask_event)

                _result = await self._run_tool(_name, _args)

                _tool_loop_emit.set(None)

                # Flush any ask_user events (agent_asking/agent_resumed)
                for _aev in _ask_events:
                    yield _aev

                # ── on_tool_result callback ──
                if self._on_tool_result:
                    _modified = await self._on_tool_result(_name, _args, _result, state)
                    if _modified is not None:
                        _result = _modified

                yield await self._emit(_make_tool_result_event(_name, _result))

                # ── Error tracking ──
                if tool_call_failed(_result, _name):
                    state.tool_errors += 1
                if _name in _tool_names_for_guard and not tool_call_failed(_result, _name):
                    state.verify_mutation_serial += 1
                if _name == "run_bash" and not _run_bash_failed(_result):
                    state.verify_last_ok_serial = max(state.verify_last_ok_serial, state.verify_mutation_serial)

                _tool_messages.append({
                    "role": "tool",
                    "content": _result,
                    "tool_call_id": _tc.get("id", _name),
                    "name": _name,
                })

            # ── K2: abort round after unknown tool ──
            if _unknown_tool_abort:
                continue

            # ── after_round callback ──
            if self._on_after_round:
                _should_continue = await self._on_after_round(_msg, _tool_messages, _round, state)
                if not _should_continue:
                    state.stop_reason = state.stop_reason or "callback_abort"
                    break

        # ── Final verification guard for openai_agent ──
        if self.cfg.verify_guard and state.verify_mutation_serial > state.verify_last_ok_serial:
            state.stop_reason = "verification_required_after_write"
            yield {"type": "token", "content": "Verification required: latest file mutations were not followed by a successful run_bash."}

        if not state.stop_reason and state.rounds_used >= self.cfg.max_rounds:
            state.stop_reason = "max_tool_rounds"

        if state.rounds_used >= self.cfg.max_rounds and not state.content_parts:
            _fallback = "[Tool-Agent: Max rounds reached, no output.]"
            state.content_parts.append(_fallback)
            yield {"type": "token", "content": _fallback}

        self.state = state

    # ── Internal helpers ──────────────────────────────────────────────

    async def _emit(self, event: dict) -> dict:
        return event

    async def _run_tool(self, name: str, args: dict) -> str:
        """Execute a tool, using custom_executor if provided."""
        if self._custom_executor:
            return await self._custom_executor(name, args)
        return await _run_inline_tool(
            name, args,
            workspace_lock=self._workspace,
            tool_mode=self.cfg.tool_mode,
            include_websearch=self.cfg.include_websearch,
        )

    async def _post_with_retry(self, payload: dict) -> tuple[dict | None, str, list[dict], list[dict]]:
        """POST to llama-server with retry loop. Returns (msg, content_text, tool_calls, events)."""
        _events: list[dict] = []
        for _attempt in range(self.cfg.max_post_attempts):
            try:
                _timeout = httpx.Timeout(
                    connect=10.0, read=self.cfg.read_timeout_s, write=10.0, pool=5.0
                )
                async with self._client.stream(
                    "POST",
                    f"http://127.0.0.1:{self._port}/v1/chat/completions",
                    json=payload,
                    timeout=_timeout,
                ) as _resp:
                    if self.cfg.retry_on_5xx and _resp.status_code in (500, 502, 503, 504) and _attempt < self.cfg.max_post_attempts - 1:
                        await _resp.aread()
                        await asyncio.sleep(1.5 + min(1.0, _attempt * 0.1))
                        _events.append({"type": "status", "content": f"⏳ Server {_resp.status_code}, retry {_attempt+1}/{self.cfg.max_post_attempts}…"})
                        continue

                    if self.cfg.retry_on_404 and _resp.status_code == 404 and _attempt < self.cfg.max_post_attempts - 1:
                        await _resp.aread()
                        await asyncio.sleep(2.0)
                        _events.append({"type": "status", "content": f"⚠ HTTP 404, retry {_attempt+1}…"})
                        continue

                    if _resp.status_code >= 400:
                        await _resp.aread()
                        _err_body = _resp.text[:300]
                        raise RuntimeError(f"Tool-POST HTTP {_resp.status_code}: {_err_body}")

                    # ── Parse non-streaming response ──
                    return self._parse_nonstream(_resp, _events)

            except httpx.ConnectError:
                if self.cfg.retry_on_connect and _attempt < self.cfg.max_post_attempts - 1:
                    await asyncio.sleep(5.0)
                    _events.append({"type": "status", "content": f"⟳ Connection error, retry {_attempt+2}…"})
                    continue
                raise

            except (httpx.ReadTimeout, httpx.ReadError):
                if self.cfg.retry_on_read_timeout and _attempt < self.cfg.max_post_attempts - 1:
                    await asyncio.sleep(3.0)
                    _events.append({"type": "status", "content": f"⚠ Read timeout, retry {_attempt+1}…"})
                    continue
                raise

        raise RuntimeError(f"Tool-POST: all {self.cfg.max_post_attempts} attempts failed")

    async def _post_stream_with_retry(self, payload: dict):
        """POST with retry for streaming — yields live token events, ends with (msg, content, tcs) tuple."""
        _fdbk_injected_this_call = False
        for _attempt in range(self.cfg.max_post_attempts):
            try:
                _timeout = httpx.Timeout(
                    connect=10.0, read=self.cfg.read_timeout_s, write=10.0, pool=5.0
                )
                async with self._client.stream(
                    "POST",
                    f"http://127.0.0.1:{self._port}/v1/chat/completions",
                    json=payload,
                    timeout=_timeout,
                ) as _resp:
                    if self.cfg.retry_on_5xx and _resp.status_code in (500, 502, 503, 504) and _attempt < self.cfg.max_post_attempts - 1:
                        await _resp.aread()
                        await asyncio.sleep(1.5 + min(1.0, _attempt * 0.1))
                        yield {"type": "status", "content": f"⏳ Server {_resp.status_code}, retry {_attempt+1}/{self.cfg.max_post_attempts}…"}
                        continue

                    if self.cfg.retry_on_404 and _resp.status_code == 404 and _attempt < self.cfg.max_post_attempts - 1:
                        await _resp.aread()
                        await asyncio.sleep(2.0)
                        yield {"type": "status", "content": f"⚠ HTTP 404, retry {_attempt+1}…"}
                        continue

                    if self.cfg.retry_on_grammar_crash and _resp.status_code == 500 and _attempt < self.cfg.max_post_attempts - 1:
                        await _resp.aread()
                        _err_body = _resp.text[:300]
                        if "CallExpression" in _err_body:
                            if not _fdbk_injected_this_call:
                                _inject_retry_feedback(payload["messages"], error_type="parse_error", bad_content="")
                                _fdbk_injected_this_call = True
                            if self.cfg.thinking:
                                self.cfg.thinking = False
                                self.cfg.thinking_budget = 0
                                payload.pop("thinking", None)
                                payload.pop("thinking_budget", None)
                                # ── L1: inject grammar now that thinking is off ──
                                if _USE_GBNF_GRAMMAR and _grammar_compatible(payload):
                                    _tn = [t["function"]["name"] for t in payload.get("tools", [])]
                                    _g = _build_tool_call_grammar(_tn)
                                    if _g:
                                        payload["grammar"] = _g
                                yield {"type": "status", "content": "⚠ Grammar-Crash (CallExpression) — retry without thinking…"}
                                continue
                            payload.pop("cache_prompt", None)
                            payload.pop("min_p", None)
                            payload.pop("chat_template_kwargs", None)
                            yield {"type": "status", "content": "⚠ Grammar-Crash — stripping extras and retry…"}
                            continue

                    if self.cfg.retry_on_parse_error and _resp.status_code == 500 and _attempt < self.cfg.max_post_attempts - 1:
                        await _resp.aread()
                        _err_body = _resp.text[:300]
                        if "parse" in _err_body.lower():
                            if not _fdbk_injected_this_call:
                                _inject_retry_feedback(payload["messages"], error_type="parse_error", bad_content="")
                                _fdbk_injected_this_call = True
                            yield {"type": "status", "content": "⚠ JSON parse error — retry…"}
                            continue

                    if self.cfg.retry_on_context_overflow and _resp.status_code == 400 and _attempt < self.cfg.max_post_attempts - 1:
                        await _resp.aread()
                        _err_body = _resp.text[:300]
                        if any(k in _err_body.lower() for k in ("exceed", "context", "token", "prompt")):
                            yield {"type": "status", "content": "⚠ Context overflow — compress and retry…"}
                            yield {"type": "ctx_overflow", "content": "compress"}
                            continue

                    # GRAMMAR-GUARD recovery (2026-08-25): 400 'Cannot use custom
                    # grammar' -> strip grammar + retry immediately
                    if _resp.status_code == 400 and payload.get("grammar") and _attempt < self.cfg.max_post_attempts - 1:
                        await _resp.aread()
                        if "grammar" in (_resp.text or "").lower():
                            payload.pop("grammar", None)
                            yield {"type": "status", "content": "⚠ Grammar+tools rejected by server — stripped grammar, retrying…"}
                            continue

                    if _resp.status_code >= 400:
                        await _resp.aread()
                        _err_body = _resp.text[:300]
                        raise RuntimeError(f"Tool-POST HTTP {_resp.status_code}: {_err_body}")

                    # Parse SSE stream, yielding live tokens
                    content_parts: list[str] = []
                    tool_calls_acc: dict[int, dict] = {}
                    async for _line in _resp.aiter_lines():
                        if not _line.startswith("data:"):
                            continue
                        _data = _line[5:].strip()
                        if _data == "[DONE]":
                            break
                        try:
                            _chunk = json.loads(_data)
                        except Exception:
                            continue
                        _choices = _chunk.get("choices") or [{}]
                        _delta = _choices[0].get("delta", {})

                        _cont, _ = _parse_sse_delta(_delta, tool_calls_acc=tool_calls_acc)
                        if _cont:
                            content_parts.append(_cont)
                            yield {"type": "token", "content": _cont}

                    _tcs_sorted = [tool_calls_acc[k] for k in sorted(tool_calls_acc.keys())]
                    _content = "".join(content_parts).strip()
                    if _content and "<think" in _content:
                        _content = _RE_THINK_CLEANUP.sub("", _content).strip()
                    _msg = {
                        "role": "assistant",
                        "content": _content or None,
                        "tool_calls": _tcs_sorted,
                    }
                    yield (_msg, _content or "", _tcs_sorted)
                    return

            except httpx.ConnectError:
                if self.cfg.retry_on_connect and _attempt < self.cfg.max_post_attempts - 1:
                    await asyncio.sleep(5.0)
                    yield {"type": "status", "content": f"⟳ Connection error, retry {_attempt+2}…"}
                    continue
                raise

            except (httpx.ReadTimeout, httpx.ReadError):
                if self.cfg.retry_on_read_timeout and _attempt < self.cfg.max_post_attempts - 1:
                    await asyncio.sleep(3.0)
                    yield {"type": "status", "content": f"⚠ Read timeout, retry {_attempt+1}…"}
                    continue
                raise
        raise RuntimeError(f"Tool-POST: all {self.cfg.max_post_attempts} attempts failed")

    def _parse_nonstream(self, resp, events: list[dict]) -> tuple[dict, str, list[dict], list[dict]]:
        """Parse non-streaming JSON response."""
        data = resp.json()
        if "choices" in data:
            msg = data["choices"][0].get("message", {}) if data["choices"] else {}
        else:
            msg = data.get("message", {})
        tool_calls = msg.get("tool_calls", [])
        content = msg.get("content", "") or ""
        return msg, content, tool_calls, events
