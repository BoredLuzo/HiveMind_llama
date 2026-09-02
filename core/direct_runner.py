import asyncio
import logging
import time
from datetime import datetime


from learning.peer_ratings import run_peer_ratings
from vram.loader import _refresh_judge_keepalive
from routing.model_picker import _pick_direct_model
from vram.loader import _bk_pin

_direct_log = logging.getLogger("hivemind.direct_runner")

_DIRECT_WS_NOTE = (
    "\n\nWEB SEARCH IS AVAILABLE: use the web_search / web_fetch tool calls "
    "for anything about recent events, external information or questions you "
    "cannot answer from your training data. Call the tools BEFORE answering."
)

def _direct_tools_note(tool_mode: str, ws_ok: bool = False) -> str:
    """TIER-AWARE (2026-09-02): only advertise tools the selected direct tier
    actually provides. The old note always listed read_file + web, which was
    wrong for the Websearch-only tier (models then called read_file and hit
    TOOL_NOT_ALLOWED)."""
    if tool_mode == "direct":
        _list = ("web_search / web_fetch" if ws_ok
                 else "no tools (web search is currently unavailable)")
    elif tool_mode == "direct_python":
        _list = ("read_file, list_dir, find_files, search_code, get_signatures, "
                 "find_references, run_python" + (", web_search / web_fetch" if ws_ok else ""))
    else:  # direct_full
        _list = ("read/write/edit tools (read_file, edit_file, write_file, ...), "
                 "run_python, run_bash" + (", web_search / web_fetch" if ws_ok else ""))
    return (
        "\n\nTOOLS ARE AVAILABLE: " + _list + ".\n"
        "Use tools ONLY when they actually help — for pure knowledge, conversation, "
        "math or general questions answer directly from your knowledge. "
        "Never call a tool that is not listed above."
    )

_DIRECT_TIME_NOTE = (
    "\n\nCURRENT DATE/TIME (local, set by the system):\n"
    "  {dt}\n"
    "Use this for any date/time questions. You can also call get_datetime() "
    "for the same info as a tool call."
)


class _DirectToolsResult:
    """Mutable outcome holder for a live direct-tools run.

    The generator streams events while it runs; after exhaustion `content` and
    `final_msgs` are filled for the fallback plain stream / persistence.
    """
    __slots__ = ("content", "final_msgs")

    def __init__(self) -> None:
        self.content = ""
        self.final_msgs = None


async def _run_direct_tools(ctx, model: str, msgs: list, tool_mode: str,
                            result: _DirectToolsResult):
    """Run the unified ToolLoop for the direct chat, STREAMING events live.

    Yields ToolLoop events (tokens, tool chips, results) as they happen.
    Previously the whole loop was awaited first and all events emitted only
    after the loop finished — the UI showed nothing until the run was done.

    After the generator is exhausted, result.content / result.final_msgs hold
    the loop outcome (for persistence and the abort/round-cap fallback).
    """
    try:
        from tools.definitions import _get_inline_tools as _tools_fn
        from backend.llama_client import manager as _mgr
        from core.tool_loop import ToolLoop, ToolLoopConfig
        import httpx as _httpx
    except Exception as _e:
        _direct_log.warning("[Direct-Tools] unavailable: %s", _e)
        yield {"type": "status", "content": f"⚠ Tool loop unavailable: {str(_e)[:120]}"}
        return

    try:
        _ws_avail = bool(ctx.websearch_available)
        _tools = _tools_fn(include_websearch=_ws_avail, mode=tool_mode)
        if not _tools:
            return
        _num_ctx = ctx.get_num_ctx(model, "direct") or 8192
        _port = await _mgr.ensure_loaded(model, num_ctx=_num_ctx)
        _agent = ctx.pipeline.agents["direct"]
        _think = bool(getattr(_agent, "thinking", False))
        _budget = int(getattr(_agent, "thinking_budget", 0) or 0)
        _max_tokens = max(int(getattr(_agent, "max_tokens", 600) or 600) + 400, 1000)
        try:
            _rounds_raw = int(ctx.settings.get("direct_tools_max_rounds", 12))
        except (TypeError, ValueError):
            _rounds_raw = 12
        _rounds = max(1, min(300, _rounds_raw))
        _read_timeout = float(ctx.settings.get("duo_llm_slow_timeout_s", 300) or 300)

        _final_msgs: list | None = None

        async def _capture_msgs(messages, round_num, state):
            nonlocal _final_msgs
            _final_msgs = list(messages)
            return messages, None

        _loop = ToolLoop(
            config=ToolLoopConfig(
                stream=True, max_rounds=_rounds, max_post_attempts=1,
                read_timeout_s=_read_timeout,
                model=model, temperature=getattr(_agent, "temperature", 0.4),
                max_tokens=_max_tokens, num_ctx=_num_ctx,
                thinking=_think, thinking_budget=_budget,
                tools=_tools, tool_mode=tool_mode,
                include_websearch=_ws_avail,
                # Direct chat: a plain answer without tools is a valid final answer.
                require_tool_call=False,
                retry_on_5xx=True, retry_on_grammar_crash=True,
                retry_on_context_overflow=True,
            ),
            http_client=_httpx.AsyncClient(timeout=_httpx.Timeout(
                connect=10.0, read=_read_timeout + 30.0, write=10.0, pool=5.0,
            )),
            port=_port,
            workspace=getattr(ctx, "workspace", "") or "",
            abort_check=ctx.aborted,
            on_before_post=_capture_msgs,
        )
        try:
            async for _ev in _loop.run(msgs):
                yield _ev
        finally:
            try:
                await _loop._client.aclose()
            except Exception:
                pass
        _state = getattr(_loop, "state", None)
        result.content = "".join(_state.content_parts).strip() if _state is not None else ""
        result.final_msgs = _final_msgs
    except Exception as _e:
        _direct_log.warning("[Direct-Tools] tool loop failed: %s", _e)
        yield {"type": "status", "content": f"⚠ Tool loop error: {str(_e)[:120]}"}


async def _direct_websearch_tool_round(ctx, model: str, msgs: list, num_ctx: int,
                                       max_rounds: int = 2) -> tuple[list, list, bool]:


    events: list = []
    try:
        from tools.definitions import _get_inline_tools as _ws_tools_fn
        from tools.handlers import (
            _inline_tool_web_search as _ws_search_fn,
            _inline_tool_web_fetch as _ws_fetch_fn,
        )
        from backend.llama_client import manager as _ws_mgr
        import httpx as _ws_httpx
        import json as _ws_json
    except Exception as _e:
        _direct_log.warning("[Direct-WS] tool round unavailable: %s", _e)
        return events, msgs, False

    _tools = [
        t for t in _ws_tools_fn(include_websearch=True)
        if t.get("function", {}).get("name") in ("web_search", "web_fetch")
    ]
    if not _tools:
        return events, msgs, False

    try:
        _port = await _ws_mgr.ensure_loaded(model, num_ctx=num_ctx)
    except Exception as _e:
        _direct_log.warning("[Direct-WS] model load failed: %s", _e)
        return events, msgs, False

    _msgs = list(msgs)
    _any = False
    for _round in range(max_rounds):
        _payload = {
            "model": model,
            "messages": _msgs,
            "tools": _tools,
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 500,
        }
        try:
            async with _ws_httpx.AsyncClient(
                timeout=_ws_httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
            ) as _c:
                _r = await _c.post(f"http://127.0.0.1:{_port}/v1/chat/completions", json=_payload)
                _r.raise_for_status()
                _msg = (_r.json() or {}).get("choices", [{}])[0].get("message", {})
        except Exception as _e:
            _direct_log.debug("[Direct-WS] round %d failed: %s", _round, _e)
            break
        _tcs = _msg.get("tool_calls") or []
        if not _tcs:
            break
        _msgs.append({"role": "assistant", "content": _msg.get("content") or None,
                      "tool_calls": _tcs})
        _any = True
        for _tc in _tcs:
            _fn = ((_tc.get("function") or {}).get("name") or "").strip()
            try:
                _args = _ws_json.loads((_tc.get("function") or {}).get("arguments") or "{}")
                if not isinstance(_args, dict):
                    _args = {}
            except Exception:
                _args = {}
            _ws_what = str(_args.get("query") or _args.get("url") or "")[:120]
            events.append(await ctx.emit({
                "type": "status", "content": f"🔍 {_fn}: {_ws_what}",
            }))
            try:
                if _fn == "web_search":
                    _res = await _ws_search_fn(_args, None, None)
                elif _fn == "web_fetch":
                    _res = await _ws_fetch_fn(_args, None, None)
                else:
                    _res = f"[{_fn} is not available in direct chat]"
            except Exception as _e:
                _res = f"[{_fn} failed: {type(_e).__name__}: {str(_e)[:200]}]"
            _msgs.append({
                "role": "tool",
                "tool_call_id": _tc.get("id") or f"direct_tc_{_round}_{len(_msgs)}",
                "content": str(_res)[:8000],
            })
    return events, _msgs, _any


async def run_direct(ctx):
    """Direct-Mode handler for simple/trivial complexity."""
    if ctx.complexity in ("trivial", "simple"):
        _available = list(ctx.models_cache) if ctx.models_cache else [ctx.pipeline.agents["direct"].model]
        _vision_mdl_name = ctx.vision_cfg.get("model", "") if ctx.prepro_success else ""
        if ctx.prepro_success:
            _direct_model = ctx.registry_get("direct")
            if (not _direct_model
                    or _direct_model not in _available
                    or _direct_model == _vision_mdl_name):
                _non_vision = [m for m in _available if m != _vision_mdl_name]
                _direct_model = _pick_direct_model(
                    ctx.complexity, ctx.effective_task_type,
                    _non_vision if _non_vision else _available
                )
        else:
            _direct_model = _pick_direct_model(ctx.complexity, ctx.effective_task_type, _available)

        _effective_task_for_vision = "vision" if (ctx.images and not ctx.image_description) else ctx.effective_task_type
        if ctx.images and not ctx.image_description:
            _chosen_caps = ctx.model_profile(_direct_model) if ctx.model_profile else {}
            if not bool(_chosen_caps.get("vision", False)):
                _vision_alt = _pick_direct_model(ctx.complexity, "vision", _available)
                if _vision_alt and _vision_alt != _direct_model:
                    _direct_log.info(
                        "[Direct-Vision] %r not multimodal — switching to %r for image request",
                        _direct_model, _vision_alt,
                    )
                    _direct_model = _vision_alt

        _preload_ev = await ctx.maybe_preload(_direct_model)
        if _preload_ev:
            yield _preload_ev

        agent = ctx.pipeline.agents["direct"]
        sys_p = ctx.get_effective_prompt_with_override("direct", ctx.active_preset, ctx.use_learned)
        sys_p += _DIRECT_TIME_NOTE.format(dt=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        direct_input = ctx.user_input
        if ctx.image_description:
            direct_input = ctx.user_input + f"\n\n[Image description from vision model]:\n{ctx.image_description}"

        _direct_caps = ctx.model_profile(_direct_model) if ctx.model_profile else {}
        _direct_vision = bool(_direct_caps.get("vision", False))
        _direct_images = ctx.images if (_direct_vision and ctx.images) else ctx.effective_images
        if _direct_vision and ctx.images and not ctx.image_description:
            _direct_log.debug(
                "[Direct-Vision] %r multimodal — %d raw images directly to the model",
                _direct_model, len(ctx.images),
            )
        # DIRECT-CHAT-TOOLS (2026-08-31): tiered tool use in direct chat.
        # The simple/direct mode can call real file/code/python/web tools via
        # the unified ToolLoop. Pure knowledge/conversation stays a plain token
        # stream (the model decides; no tool call => no round trip).
        _direct_tools_enabled = bool(ctx.settings.get("direct_tools_enabled", True))
        _direct_tier = str(ctx.settings.get("direct_tools_tier", "readonly") or "readonly").strip().lower()
        _direct_tool_mode = {
            "readonly": "direct",
            "python": "direct_python",
            "full": "direct_full",
        }.get(_direct_tier, "")
        _direct_tc = _direct_caps.get("tool_call") is not False
        _direct_tools_active = bool(_direct_tool_mode) and _direct_tools_enabled and _direct_tc
        # tier="off" (or any unrecognised value) = total pure chat: no tool
        # loop AND no legacy websearch mini-round / inject in the direct chat.
        _direct_tier_off = not bool(_direct_tool_mode)

        # P2 + DIRECT-WS-TOOLCALLS (2026-08-27): Web search in direct chat.
        # Research mode: the model may call web_search/web_fetch as real tool
        # calls — active when duo_websearch_enabled OR pipeline_websearch_enabled
        # (default on) and SearXNG is reachable. pipeline_websearch_enabled also
        # serves as fallback injection if no tool call happened.
        # When the direct tool loop is active, web is part of its tool set and
        # the legacy mini-round / inject are skipped (avoid double search).
        _direct_ws_tool = bool(
            (ctx.settings.get("duo_websearch_enabled", False)
             or ctx.settings.get("pipeline_websearch_enabled", False))
            and ctx.websearch_available
        ) and not _direct_tools_active and not _direct_tier_off
        _direct_ws_inject = bool(
            ctx.settings.get("pipeline_websearch_enabled", False) and ctx.websearch_available
        ) and not _direct_tools_active and not _direct_tier_off
        if (_direct_ws_tool or (_direct_tools_active and ctx.websearch_available)) \
                and "WEB SEARCH IS AVAILABLE" not in sys_p:
            sys_p += _DIRECT_WS_NOTE
        if _direct_tools_active:
            sys_p += _direct_tools_note(_direct_tool_mode,
                                        ws_ok=bool(ctx.websearch_available))
        messages = ctx.make_messages(ctx.pipeline, sys_p, direct_input, _direct_images, True, True, cached_mem_ctx=ctx.pipeline_mem_ctx, cached_sess_msgs=ctx.pipeline_sess_msgs)
        # FIX (2026-09-01): emit the "Answer" agent event BEFORE the direct
        # tools loop. Otherwise the ToolLoop streams its token events before the
        # frontend has an S.curAgent bubble (appendToken drops tokens when
        # curAgent is null) → the answer was lost, the bubble stayed empty, and
        # the run ended "without error".
        yield await ctx.emit({"type": "agent", "content": "Answer", "model": _direct_model})
        _ws_used = False
        _tool_content = ""
        _tool_final_msgs = None
        if _direct_tools_active:
            _dt_result = _DirectToolsResult()
            # LIVE-STREAM (2026-09-01): pass ToolLoop events through immediately —
            # previously _run_direct_tools collected all events and emitted them
            # only after the loop ended (UI showed nothing during the run).
            async for _ev in _run_direct_tools(
                    ctx, _direct_model, messages, _direct_tool_mode, _dt_result):
                yield await ctx.emit(_ev)
            _tool_content = _dt_result.content
            _tool_final_msgs = _dt_result.final_msgs
        elif _direct_ws_tool:
            _ws_events, _direct_msgs2, _ws_used = await _direct_websearch_tool_round(
                ctx, _direct_model, messages,
                ctx.get_num_ctx(_direct_model, "direct") or 8192,
            )
            for _ev in _ws_events:
                yield _ev
            if _ws_used:
                messages = _direct_msgs2
        if _direct_ws_inject and not _ws_used:
            _direct_ws_query = ctx.extract_ws_query(ctx.user_input)
            if _direct_ws_query:
                yield await ctx.emit({"type": "status",
                                      "content": f"🔍 Web search: {_direct_ws_query[:60]}…"})
                _direct_ws_result = await ctx.safe_web_search(
                    _direct_ws_query,
                    max_results=3,
                    phase="direct",
                )
                if _direct_ws_result.startswith("[web_search:"):
                    yield await ctx.emit({
                        "type": "status",
                        "content": f"⚠ Web search fallback: {_direct_ws_result}",
                    })
                else:
                    direct_input = (
                        direct_input
                        + f"\n\n[Web search results]\n{_direct_ws_result}\n[End search results]\n"
                    )
                    messages = ctx.make_messages(ctx.pipeline, sys_p, direct_input, _direct_images, True, True, cached_mem_ctx=ctx.pipeline_mem_ctx, cached_sess_msgs=ctx.pipeline_sess_msgs)
                    yield await ctx.emit({"type": "status", "content": "✓ Web search complete"})
        _direct_temp   = agent.temperature
        _direct_tokens = agent.max_tokens
        # AGENT-THINKING (2026-08-19): Per-agent switch from AgentConfig.
        _direct_think = bool(getattr(agent, "thinking", False))
        _direct_budget = int(getattr(agent, "thinking_budget", 0) or 0)
        if _direct_think:
            _direct_tokens = _direct_tokens + _direct_budget
        # OUTPUT-CAP (2026-08-27): max_tokens never above the effective context —
        # otherwise the answer is silently truncated (finish_reason=length).
        _direct_ctx_cap = ctx.get_num_ctx(_direct_model, "direct") or 8192
        _direct_tokens = min(int(_direct_tokens), max(256, int(_direct_ctx_cap)))
        parts, t = [], time.time()
        if _direct_tools_active and _tool_content:
            # ToolLoop already streamed the answer live — reuse it for persistence.
            parts = [_tool_content]
        else:
            try:
                # _pick_direct_model(complex, ...) → qwen3:8b → 131072 default ctx → ~8GB KV-Cache → OVERFLOW
                _direct_no_cache = ctx.prepro_success or bool(_direct_vision and ctx.images)
                _stream_msgs = messages
                if _direct_tools_active and _tool_final_msgs:
                    # Tool loop ran but produced no text (abort / round cap) —
                    # answer with the tool-augmented context instead.
                    _stream_msgs = _tool_final_msgs
                if _direct_think:
                    # Thinking mode: stream content and reasoning separately.
                    async for _cont, _thk in ctx.pipeline.ollama.chat_stream(
                            _direct_model, _stream_msgs, _direct_temp, _direct_tokens,
                            ctx=ctx.get_num_ctx(_direct_model, "direct") or 8192,
                            no_cache=ctx.prepro_success,
                            think=True,
                            thinking_budget=_direct_budget or None,
                            split_thinking=True):
                        if ctx.aborted(): break
                        if _thk:
                            yield await ctx.emit({"type": "thinking_token", "content": _thk})
                        if _cont:
                            parts.append(_cont)
                            yield await ctx.emit({"type": "token", "content": _cont})
                else:
                    async for tok in ctx.pipeline.ollama.chat_stream(
                            _direct_model, _stream_msgs, _direct_temp, _direct_tokens,
                            ctx=ctx.get_num_ctx(_direct_model, "direct") or 8192,
                            no_cache=ctx.prepro_success,
                            think=False):
                        if ctx.aborted(): break
                        parts.append(tok)
                        yield await ctx.emit({"type": "token", "content": tok})
            except Exception as _direct_err:
                _err_tok = f"[Direct error: {str(_direct_err)[:120]}]"
                parts.append(_err_tok)
                yield await ctx.emit({"type": "token", "content": _err_tok})
        content = "".join(parts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
        # Content (z.B. max_tokens/thinking_budget durch Reasoning verbraucht),
        if _direct_think and not content and not ctx.aborted():
            _hint = ("[Note: The model only thought (🧠) but did not deliver an answer — "
                     "the thinking budget or max_tokens were consumed by reasoning. "
                     "Increase think-budget/max_tokens or disable thinking.]")
            yield await ctx.emit({"type": "token", "content": _hint})
            parts.append(_hint)
            content = _hint
        yield await ctx.emit({"type": "agent_done", "elapsed": round(time.time() - t, 1)})
        ctx.memory.add_to_session("user", ctx.user_input)
        ctx.memory.add_to_session("assistant", content)
        if getattr(ctx, "chat_id", None):
            try:
                from context.chat import _mutate_chat_context

                def _persist_direct_session(cdict):
                    cdict["session"] = ctx.memory.get_session_messages(
                        limit=40, user_cap=10**6, assistant_cap=10**6,
                    )
                    cdict["ts"] = time.time()

                _mutate_chat_context(ctx.chat_id, _persist_direct_session)
            except Exception as _persist_exc:
                _direct_log.warning(
                    "[Direct] Session-Persistenz fehlgeschlagen (chat=%s): %s",
                    ctx.chat_id, _persist_exc,
                )
        _direct_stop_reason = "aborted" if ctx.aborted() else "completed"
        yield await ctx.emit(ctx.done_event(round(time.time() - ctx.t_total, 1), _direct_stop_reason, **ctx.collect_done_metrics()))
        ctx.unregister_abort(ctx.run_id)
        ctx.unregister_step_skip(ctx.run_id)  # P7 FIX: was missing
        asyncio.create_task(_refresh_judge_keepalive())
        if ctx.settings.get("pin_direct_after_response", False):
            async def _pin_direct_bg():
                try:
                    await _bk_pin(_direct_model, num_ctx=ctx.get_num_ctx(_direct_model, "direct"))
                except Exception:
                    pass
            asyncio.create_task(_pin_direct_bg())
        run_count = ctx.increment_run_counter()
        asyncio.create_task(run_peer_ratings(
            run_id=ctx.run_id,
            user_input=ctx.user_input,
            outputs={"direct": content},
            use_learned=ctx.use_learned,
            rating_mode="direct",
            has_images=bool(ctx.images),
        ))
        asyncio.create_task(ctx.maybe_trigger_soul_evolution(run_count))
        return
