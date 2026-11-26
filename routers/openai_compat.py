"""OpenAI-compatible /v1/* chat completions router."""
from __future__ import annotations

import hmac
import json
import os
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.duo_helpers import _bucket_stop_reason
from tools.definitions import _get_inline_tools
from tools.errors import tool_error_response as _tool_error_response

router = APIRouter(tags=["OpenAI"])


async def _v1_agent_mode(msgs, raw_model, temp, max_tok, chat_id, created, do_stream):
    """AGENT MODE: tool-call loop for an OpenAI-compatible endpoint."""
    import server as _srv
    S_models_cache = _srv.S_models_cache
    _WEBSEARCH_AVAILABLE = _srv._WEBSEARCH_AVAILABLE
    _get_num_ctx = _srv._get_num_ctx
    _run_inline_tool = _srv._run_inline_tool
    settings = _srv.settings
    _TOOL_PRIORITY = ["granite-4.1:3b", "qwen3.5:2b", "qwen3.5:9b-ud", "rnj-1:8b"]
    explicit = raw_model[6:].lstrip(":") if ":" in raw_model.replace("agent", "", 1) else ""
    agent_model = explicit if explicit else next(
        (m for m in _TOOL_PRIORITY if m in S_models_cache),
        S_models_cache[0] if S_models_cache else "qwen3.5:9b-ud"
    )

    from tools.definitions import git_tools_enabled as _gte_openai
    _OPENAI_WS_ENABLED = bool(settings.get("duo_websearch_enabled", False)) and _WEBSEARCH_AVAILABLE
    _OPENAI_GIT_ENABLED = _gte_openai(settings)
    _CODING_TOOLS = _get_inline_tools(
        include_websearch=_OPENAI_WS_ENABLED, mode="openai_agent",
        include_git=_OPENAI_GIT_ENABLED, settings=settings)
    # H-audit: derive the lock like /internal/tool/exec does - ignoring the
    # configured workspace let agent tools run against the install dir.
    # WS-ORDER (2026-10-16): the UI-configured project wins over the
    # run-scoped env - the same order as the workspace resolver and
    # routers/core.py. The old env-first order pointed the v1 endpoint at
    # a stale/parallel run's directory while the UI showed another project.
    _OPENAI_WORKSPACE_LOCK = str(Path(
        (settings.get("workspace") or "").strip()
        or os.environ.get("HIVEMIND_WORKSPACE") or "."
    ).resolve())

    async def _execute_tool(name: str, args: dict, *, model_for_limits: str) -> str:
        if name == "hivemind_pipeline":
            query = args.get("query", "")
            mode = args.get("mode", "auto")
            try:
                tokens = []
                _hivemind_port = str(os.environ.get("HIVEMIND_PORT") or settings.get("server_port") or 8001)  # H-audit: real default
                async with httpx.AsyncClient(timeout=180) as client:
                    async with client.stream("POST",
                        f"http://localhost:{_hivemind_port}/stream",
                        json={"q": query, "mode": mode}) as resp:
                        async for line in resp.aiter_lines():
                            if line.startswith("data:"):
                                try:
                                    d = json.loads(line[5:])
                                    if d.get("type") == "token":
                                        tokens.append(d["content"])
                                except (json.JSONDecodeError, TypeError, KeyError, ValueError, AttributeError):
                                    continue
                return "".join(tokens).strip()
            except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError) as e:
                return _tool_error_response("PIPELINE_CALL_FAILED",
                    f"hivemind_pipeline failed: {type(e).__name__}: {str(e)[:220]}",
                    tool="hivemind_pipeline", mode="openai_agent")
        _tool_args = dict(args or {})
        _tool_args["__model__"] = model_for_limits
        try:
            # P1 (deep audit 2026-11-03): this OpenAI-compatible endpoint has
            # no run context by design; without the marker the approval gate
            # denied every write/exec when the toggle was on.
            from tools.runner import _external_dispatch as _ext_cv_ag
            _ext_tok_ag = _ext_cv_ag.set(True)
            try:
                return await _run_inline_tool(name, _tool_args,
                    workspace_lock=_OPENAI_WORKSPACE_LOCK,
                    tool_mode="openai_agent", include_websearch=_OPENAI_WS_ENABLED,
                    include_git=_OPENAI_GIT_ENABLED)
            finally:
                _ext_cv_ag.reset(_ext_tok_ag)
        except (httpx.HTTPError, OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError) as e:
            return _tool_error_response("TOOL_EXEC_EXCEPTION",
                f"{type(e).__name__}: {str(e)[:220]}",
                tool=name, mode="openai_agent")

    _agent_state = {
        "stop_reason": "completed", "tool_calls": 0, "tool_errors": 0,
        "last_tool_error": None,
    }
    system_msg = {"role": "system", "content": (
        "Use the available tools proactively for file analysis, code execution, and debugging.\\n"
        "For deep code analysis, combine get_signatures, find_files, and search_code before editing.\\n"
        "Prefer using tools over guessing when data or execution is required.\\n"
        "Be precise, concise, and solution-oriented.\\n"
        "Reply in the same language as the user's latest message.")}
    current_msgs = list(msgs)
    if not any(m.get("role") == "system" for m in current_msgs):
        current_msgs = [system_msg] + current_msgs

    async def _agent_loop():
        loop_msgs = list(current_msgs)
        _ag_ctx = _get_num_ctx(agent_model)
        _ag_opts: dict = {"temperature": temp, "num_predict": max_tok}
        if _ag_ctx:
            _ag_opts["num_ctx"] = _ag_ctx
        async with httpx.AsyncClient(timeout=120) as _agent_client:
            from backend.llama_server_manager import manager as _lsm4
            _ag_port = await _lsm4.ensure_loaded(agent_model, num_ctx=_ag_opts.get("num_ctx"))
            from core.tool_loop import ToolLoop, ToolLoopConfig
            _loop = ToolLoop(
                config=ToolLoopConfig(
                    stream=False, max_rounds=8, max_post_attempts=1,
                    model=agent_model, temperature=_ag_opts.get("temperature", 0.2),
                    max_tokens=_ag_opts.get("num_predict", 800),
                    num_ctx=_ag_opts.get("num_ctx", 4096),
                    tools=_CODING_TOOLS, tool_mode="openai_agent",
                    verify_guard=True,
                ),
                http_client=_agent_client, port=_ag_port,
                custom_executor=lambda name, args: _execute_tool(name, args, model_for_limits=agent_model),
            )
            async for _ev in _loop.run(loop_msgs):
                if _ev["type"] == "token":
                    yield _ev["content"]
            _agent_state["stop_reason"] = _loop.state.stop_reason
            _agent_state["tool_calls"] = _loop.state.tool_calls_made
            _agent_state["tool_errors"] = _loop.state.tool_errors

            if _loop.state.stop_reason == "completed":
                return
            if _loop.state.stop_reason in ("verification_required_after_write", "max_tool_rounds"):
                yield ("Verification required: latest file mutations were not followed by a successful run_bash."
                       if _loop.state.stop_reason == "verification_required_after_write"
                       else "[Max tool rounds reached]")
                return

        _agent_state["stop_reason"] = "max_tool_rounds"
        try:
            from backend.llama_server_manager import manager as _lsm4b
            _final_msgs = list(loop_msgs) + [{"role": "user", "content": (
                "Tool round limit reached. Provide the best possible final answer now. Do not call tools.")}]
            _ag_port2 = await _lsm4b.ensure_loaded(agent_model, num_ctx=_ag_opts.get("num_ctx"))
            async with httpx.AsyncClient(timeout=120) as _fc:
                _final_resp = await _fc.post(
                    f"http://127.0.0.1:{_ag_port2}/v1/chat/completions",
                    json={"model": agent_model, "messages": _final_msgs, "stream": False,
                          "temperature": _ag_opts.get("temperature", 0.2),
                          "max_tokens": _ag_opts.get("num_predict", 800)})
                _final_data = _final_resp.json()
            if "choices" in _final_data:
                _final_msg = (_final_data["choices"][0].get("message") or {}) if _final_data["choices"] else {}
            else:
                _final_msg = _final_data.get("message") or {}
            _final_txt = str(_final_msg.get("content", "") or "").strip()
            if _final_txt:
                yield _final_txt; return
        except (httpx.HTTPError, OSError, RuntimeError, json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError):
            yield "[Max tool rounds reached]"
            return
        yield "[Max tool rounds reached]"

    def _stop_bucket(sr):
        return _bucket_stop_reason(sr)

    if do_stream:
        async def _agent_stream():
            async for chunk in _agent_loop():
                yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': raw_model, 'choices': [{'index': 0, 'delta': {'content': chunk}, 'finish_reason': None}]})}\n\n"
            yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': raw_model, 'stop_reason': _agent_state['stop_reason'], 'stop_reason_bucket': _stop_bucket(_agent_state['stop_reason']), '_meta': {'tool_calls': _agent_state['tool_calls'], 'tool_errors': _agent_state['tool_errors'], 'last_tool_error': _agent_state['last_tool_error']}, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_agent_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    parts = []
    async for chunk in _agent_loop():
        parts.append(chunk)
    _ag_est = max(len(parts), len("".join(parts)) // 3) if parts else 0
    return {"id": chat_id, "object": "chat.completion", "created": created, "model": raw_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(parts)}, "finish_reason": "stop"}],
            "stop_reason": _agent_state["stop_reason"],
            "stop_reason_bucket": _stop_bucket(_agent_state["stop_reason"]),
            "_meta": {"tool_calls": _agent_state["tool_calls"], "tool_errors": _agent_state["tool_errors"], "last_tool_error": _agent_state["last_tool_error"]},
            "usage": {"prompt_tokens": 0, "completion_tokens": _ag_est, "total_tokens": _ag_est}}


async def _v1_direct_mode(msgs, raw_model, temp, max_tok, chat_id, created, do_stream):
    """DIRECT MODE (2026-08-31): simple chat via run_stream.

    Runs in-process through the same direct path as the UI (incl. optional
    chat tools) and returns OpenAI-compatible answers.
    """
    import server as _srv
    run_stream = _srv.run_stream
    settings = _srv.settings
    _last_user_idx = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
    if _last_user_idx < 0 or not str(msgs[_last_user_idx].get("content") or "").strip():
        return JSONResponse({"error": "no user message"}, status_code=400)
    _user_msg = str(msgs[_last_user_idx].get("content") or "")
    _prior_msgs = [m for m in msgs[:_last_user_idx] if m.get("content")]
    if _prior_msgs:
        _hist_lines = "\n".join(
            f"[{str(m.get('role') or 'user').title()}]: {m.get('content')}" for m in _prior_msgs
        )
        _user_msg = f"Conversation history:\n{_hist_lines}\n\n[User]: {_user_msg}"

    _direct_overrides: dict = {}
    _direct_agent_ovr: dict = {}
    _v1_model = str(raw_model or "").strip()
    if _v1_model and _v1_model.lower() not in ("direct", "default", "auto", "agent"):
        _direct_overrides["direct_model"] = _v1_model
        _direct_agent_ovr["model"] = _v1_model
    if temp is not None:
        _direct_agent_ovr["temperature"] = float(temp)
        _direct_overrides["agents_user_sampling_choice"] = {"direct": ["temperature"]}
    if max_tok is not None and int(max_tok) > 0:
        _direct_agent_ovr["max_tokens"] = int(max_tok)
    if _direct_agent_ovr:
        _base_agents = dict(settings.get("agents") or {})
        _dir_cfg = dict(_base_agents.get("direct") or {})
        _dir_cfg.update(_direct_agent_ovr)
        _base_agents["direct"] = _dir_cfg
        _direct_overrides["agents"] = _base_agents

    _v1_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    async def _direct_loop():
        _parts: list = []
        try:
            async for _chunk in run_stream(
                _user_msg, [], "simple", 1, None,
                constraint_mode=True, force_complexity="simple", chat_id=None,
                model_overrides=_direct_overrides or None,
            ):
                _s = _chunk if isinstance(_chunk, str) else ""
                if not _s.startswith("data: "):
                    continue
                try:
                    _d = json.loads(_s[6:])
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if _d.get("type") == "usage_meta":
                    _v1_usage["prompt_tokens"] += int(_d.get("prompt_tokens") or 0)
                    _v1_usage["completion_tokens"] += int(_d.get("completion_tokens") or 0)
                elif _d.get("type") == "token" and _d.get("content"):
                    _txt = str(_d["content"])
                    _parts.append(_txt)
                    yield _txt
                elif _d.get("type") == "done":
                    if _d.get("prompt_tokens_total"):
                        _v1_usage["prompt_tokens"] = int(_d["prompt_tokens_total"])
                    if _d.get("tokens_generated"):
                        _v1_usage["completion_tokens"] = int(_d["tokens_generated"])
                    return
        except (httpx.HTTPError, OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError) as _e:
            _err = f"[direct error: {type(_e).__name__}: {str(_e)[:200]}]"
            _parts.append(_err)
            yield _err

    if do_stream:
        async def _direct_stream():
            async for chunk in _direct_loop():
                yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': raw_model, 'choices': [{'index': 0, 'delta': {'content': chunk}, 'finish_reason': None}]})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_direct_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    parts = []
    async for chunk in _direct_loop():
        parts.append(chunk)
    _pt = int(_v1_usage["prompt_tokens"] or 0)
    _ct = int(_v1_usage["completion_tokens"] or 0) or (max(1, len("".join(parts)) // 3) if parts else 0)
    return {"id": chat_id, "object": "chat.completion", "created": created, "model": raw_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(parts)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": _pt, "completion_tokens": _ct, "total_tokens": _pt + _ct}}


@router.post("/v1/chat/completions")
async def v1_chat_completions(req: Request):
    """OpenAI-kompatibler /v1/chat/completions Endpunkt."""
    import server as _srv
    settings = _srv.settings
    body = await req.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    _default_model = settings.get("agents", {}).get("direct", {}).get("model", "qwen3.5:9b-ud")
    raw_model = str(body.get("model") or _default_model)
    msgs = body.get("messages") or []
    if not isinstance(msgs, list) or not msgs:
        return JSONResponse({"error": "messages missing"}, status_code=400)
    do_stream = bool(body.get("stream", False))
    _raw_max = body.get("max_tokens")
    if _raw_max is None:
        _raw_max = body.get("max_completion_tokens")
    try:
        max_tok = int(_raw_max) if _raw_max is not None else 4096
    except (TypeError, ValueError):
        return JSONResponse({"error": "max_tokens must be an integer"}, status_code=400)
    _raw_temp = body.get("temperature")
    try:
        temp = float(_raw_temp) if _raw_temp is not None else 0.2
    except (TypeError, ValueError):
        return JSONResponse({"error": "temperature must be a number"}, status_code=400)

    chat_id = f"chatcmpl-{int(time.time())}"
    created = int(time.time())

    use_pipeline = raw_model.startswith("pipeline")
    use_agent    = raw_model == "agent" or raw_model.startswith("agent:")

    if use_agent:
        # V1-AGENT-TOKEN (audit 2026-11-03): agent mode hands the local model
        # run_bash/run_python/MCP tools. Without a token every local process
        # could drive them (and _external_dispatch skips the approval gate).
        # Same policy as /internal/tool/exec: disabled unless configured.
        _v1_tok = os.environ.get("HIVEMIND_INTERNAL_TOKEN", "").strip()
        if not _v1_tok:
            return JSONResponse(
                {"error": "agent mode is disabled without HIVEMIND_INTERNAL_TOKEN "
                          "(read-only /v1 chat stays available)"},
                status_code=403)
        _v1_given = req.headers.get("x-hivemind-internal-token", "").strip()
        if not hmac.compare_digest(_v1_given, _v1_tok):
            return JSONResponse(
                {"error": "invalid X-Hivemind-Internal-Token"}, status_code=403)
        return await _srv._v1_agent_mode(msgs, raw_model, temp, max_tok, chat_id, created, do_stream)
    elif use_pipeline:
        return JSONResponse({"error": "pipeline mode not yet implemented"}, status_code=501)
    else:
        return await _srv._v1_direct_mode(msgs, raw_model, temp, max_tok, chat_id, created, do_stream)


