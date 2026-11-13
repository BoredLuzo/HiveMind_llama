# -*- coding: utf-8 -*-
"""Welle 3 Stufe A: shared HTTP transport, retry, SSE, usage, and caller delegation checks."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="results", pad=True)
check = h.check

from core import llm_transport as T  # noqa: E402
from core.tool_loop import ToolLoop, ToolLoopConfig  # noqa: E402


async def _no_sleep(_sec: float) -> None:
    return None


def _mk_client(responses: list) -> tuple[httpx.AsyncClient, dict]:
    calls = {"n": 0, "payloads": []}

    def _handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        try:
            import json
            calls["payloads"].append(json.loads(request.content.decode("utf-8") or "{}"))
        except ValueError:
            calls["payloads"].append({})
        idx = min(calls["n"] - 1, len(responses) - 1)
        nxt = responses[idx]
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return client, calls


# ── 1. Pure transport & payload helpers ────────────────────────────────────
check("overflow marker detection",
      T.is_context_overflow_body("Prompt exceeds context window") is True
      and T.is_context_overflow_body("internal template error") is False)
check("F3 plain 500 caps at 3 attempts (0/1 True, 2 False)",
      T.should_retry_http_status(500, "err", 0, 10) is True
      and T.should_retry_http_status(500, "err", 1, 10) is True
      and T.should_retry_http_status(500, "err", 2, 10) is False)
check("F3 overflow 500 never retries",
      T.should_retry_http_status(500, "too many tokens", 0, 10) is False)
check("F3 503 retries up to max_attempts",
      T.should_retry_http_status(503, "loading", 8, 10) is True)

_orig_pl = {"thinking": True, "thinking_budget": 1000, "chat_template_kwargs": {"foo": 1, "enable_thinking": True}}
_dis_pl = T.disable_thinking_in_payload(_orig_pl, max_tokens=512)
check("disable_thinking_in_payload returns non-mutated disabled copy",
      _orig_pl["thinking"] is True
      and _dis_pl["thinking"] is False
      and _dis_pl["thinking_budget"] == 0
      and _dis_pl["max_tokens"] == 512
      and _dis_pl["chat_template_kwargs"] == {"foo": 1, "enable_thinking": False})

_stripped = T.strip_payload_extras({"cache_prompt": True, "min_p": 0.05, "temperature": 0.2})
check("strip_payload_extras pops cache_prompt + min_p and disables thinking",
      "cache_prompt" not in _stripped and "min_p" not in _stripped
      and _stripped["chat_template_kwargs"]["enable_thinking"] is False
      and _stripped["temperature"] == 0.2)

_u1 = T.parse_usage({"prompt_tokens": 120, "completion_tokens": 45, "prompt_tokens_details": {"cached_tokens": 80}}, gen_ms=250, decode_tps=32.5)
_u2 = T.parse_usage({"prompt_tokens": 50, "completion_tokens": 10, "cache_n": 30})
check("parse_usage extracts nested cached_tokens and cache_n",
      _u1 is not None and _u1["cached_tokens"] == 80 and _u1["decode_tps"] == 32.5
      and _u2 is not None and _u2["cached_tokens"] == 30)


# ── 2. post_chat_json retries, overflow fast-fail & ConnectError recovery ──
async def _test_post_chat_json() -> None:
    req_url = "http://127.0.0.1:9999/v1/chat/completions"

    # (a) 500 -> 200 recovery
    c1, st1 = _mk_client([
        httpx.Response(500, text="blip", request=httpx.Request("POST", req_url)),
        httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "cache_n": 10},
        }, request=httpx.Request("POST", req_url)),
    ])
    try:
        with mock.patch("asyncio.sleep", new=_no_sleep):
            res = await T.post_chat_json(9999, {"messages": [{"role": "user", "content": "hi"}]},
                                         max_attempts=3, client=c1, log_label="test")
        check("post_chat_json recovers after transient 500",
              res.content_text == "ok" and st1["n"] == 2 and res.usage is not None and res.usage["cached_tokens"] == 10)
    finally:
        await c1.aclose()

    # (b) overflow 500 fails after 1 POST (F3 policy)
    c2, st2 = _mk_client([
        httpx.Response(500, text="prompt exceeds context window", request=httpx.Request("POST", req_url)),
    ] * 3)
    try:
        err_msg = ""
        try:
            with mock.patch("asyncio.sleep", new=_no_sleep):
                await T.post_chat_json(9999, {"messages": []}, max_attempts=3, client=c2, raise_on_status=False, log_label="test")
        except RuntimeError as exc:
            err_msg = str(exc)
        check("post_chat_json fails immediately on overflow 500",
              st2["n"] == 1 and "context window" in err_msg)
    finally:
        await c2.aclose()

    # (c) ConnectError -> retry -> 200
    c3, st3 = _mk_client([
        httpx.ConnectError("refused"),
        httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "recovered"}}]},
                       request=httpx.Request("POST", req_url)),
    ])
    try:
        with mock.patch("asyncio.sleep", new=_no_sleep):
            res3 = await T.post_chat_json(9999, {"messages": []}, max_attempts=3, client=c3, log_label="test")
        check("post_chat_json recovers from ConnectError",
              res3.content_text == "recovered" and st3["n"] == 2)
    finally:
        await c3.aclose()


asyncio.run(_test_post_chat_json())


# ── 3. stream_chat_chunks retries before first byte & skips bad SSE lines ───
async def _test_stream_chat_chunks() -> None:
    req_url = "http://127.0.0.1:9999/v1/chat/completions"
    sse_body = (
        "data: {not-valid-json}\n\n"
        'data: {"choices":[{"delta":{"content":"hello "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    c1, st1 = _mk_client([
        httpx.Response(503, text="loading slot", request=httpx.Request("POST", req_url)),
        httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"},
                       request=httpx.Request("POST", req_url)),
    ])
    try:
        tokens: list[str] = []
        with mock.patch("asyncio.sleep", new=_no_sleep):
            async for chunk in T.stream_chat_chunks(9999, {"messages": [], "stream": True},
                                                    max_attempts=3, client=c1, log_label="test"):
                tok = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                if tok:
                    tokens.append(tok)
        check("stream_chat_chunks retries 503, tolerates malformed SSE chunk, yields tokens",
              st1["n"] == 2 and "".join(tokens) == "hello world")
    finally:
        await c1.aclose()


asyncio.run(_test_stream_chat_chunks())


# ── 4. ToolLoop stream & non-stream delegation via iter_tool_loop_post ───────
async def _test_tool_loop_transport() -> None:
    req_url = "http://127.0.0.1:9999/v1/chat/completions"

    # (a) Non-stream 503 -> 400 grammar strip -> 200 with usage accumulation
    c_ns, st_ns = _mk_client([
        httpx.Response(503, text="busy", request=httpx.Request("POST", req_url)),
        httpx.Response(400, text="Cannot use custom grammar with tools", request=httpx.Request("POST", req_url)),
        httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "done", "tool_calls": []}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 25, "cached_tokens": 60},
        }, request=httpx.Request("POST", req_url)),
    ])
    try:
        loop_ns = ToolLoop(
            ToolLoopConfig(stream=False, max_post_attempts=4, model="fake:1b"),
            c_ns,
            9999,
        )
        loop_ns.state = __import__("core.tool_loop", fromlist=["ToolLoopState"]).ToolLoopState()
        loop_ns._usage_prompt = 0
        loop_ns._usage_completion = 0
        loop_ns._usage_cached = 0
        with mock.patch("asyncio.sleep", new=_no_sleep):
            msg, content, tcs, events = await loop_ns._post_with_retry({
                "model": "fake:1b",
                "messages": [{"role": "user", "content": "hi"}],
                "grammar": "root ::= \"x\"",
            })
        check("ToolLoop non-stream retries 503, strips grammar on 400, tracks usage",
              st_ns["n"] == 3 and "grammar" not in st_ns["payloads"][-1]
              and content == "done" and tcs == [] and isinstance(msg, dict)
              and loop_ns.state.prompt_tokens == 100
              and loop_ns.state.completion_tokens == 25
              and loop_ns.state.cached_tokens == 60
              and len(events) >= 2)
    finally:
        await c_ns.aclose()

    # (b) Stream CallExpression recovery + <think> cleanup
    sse_ok = (
        'data: {"choices":[{"delta":{"content":"<think>plan</think>final answer"}}],'
        '"usage":{"prompt_tokens":40,"completion_tokens":12,"cache_n":15}}\n\n'
        "data: [DONE]\n\n"
    )
    c_st, st_st = _mk_client([
        httpx.Response(500, text="grammar CallExpression crash", request=httpx.Request("POST", req_url)),
        httpx.Response(200, text=sse_ok, headers={"content-type": "text/event-stream"},
                       request=httpx.Request("POST", req_url)),
    ])
    try:
        cfg_st = ToolLoopConfig(
            stream=True, max_post_attempts=3, model="fake:1b",
            thinking=True, retry_on_grammar_crash=True, retry_on_5xx=False,
        )
        loop_st = ToolLoop(cfg_st, c_st, 9999)
        loop_st._usage_prompt = 0
        loop_st._usage_completion = 0
        loop_st._usage_cached = 0
        out_events = []
        final_tuple = None
        with mock.patch("asyncio.sleep", new=_no_sleep):
            async for ev in loop_st._post_stream_with_retry({
                "model": "fake:1b",
                "messages": [{"role": "user", "content": "hi"}],
                "thinking": True,
                "tools": [{"type": "function", "function": {"name": "read_file"}}],
            }):
                if isinstance(ev, tuple):
                    final_tuple = ev
                else:
                    out_events.append(ev)
        check("ToolLoop stream disables thinking on CallExpression 500 and yields final tuple",
              st_st["n"] == 2 and cfg_st.thinking is False
              and final_tuple is not None and final_tuple[1] == "final answer"
              and any(e.get("type") == "status" for e in out_events)
              and loop_st._usage_cached == 15)
    finally:
        await c_st.aclose()


asyncio.run(_test_tool_loop_transport())


# ── 4b. Caller delegation behavior (fix_agent, subagent_lite, 404/err) ─────
async def _test_caller_wrappers() -> None:
    from core import fix_agent as _fa
    from core import subagent_lite as _sl

    req_url = "http://127.0.0.1:9999/v1/chat/completions"
    c_fa, st_fa = _mk_client([
        httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "VERDICT: FIXED — ok"}}],
        }, request=httpx.Request("POST", req_url)),
    ])
    try:
        orig_client = httpx.AsyncClient
        with mock.patch("core.llm_transport.httpx.AsyncClient", side_effect=lambda *a, **k: c_fa):
            with mock.patch("asyncio.sleep", new=_no_sleep):
                msg_fa = await _fa._post_chat(9999, "m:1b", [{"role": "user", "content": "fix"}])
        assert orig_client is not None
        check("fix_agent._post_chat delegates via post_chat_json cleanly",
              st_fa["n"] == 1 and "VERDICT: FIXED" in str(msg_fa.get("content") or ""))
    finally:
        await c_fa.aclose()

    c_sl, st_sl = _mk_client([
        httpx.Response(503, text="loading", request=httpx.Request("POST", req_url)),
        httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "summary"}}],
        }, request=httpx.Request("POST", req_url)),
    ])
    try:
        with mock.patch("core.llm_transport.httpx.AsyncClient", side_effect=lambda *a, **k: c_sl):
            with mock.patch("asyncio.sleep", new=_no_sleep):
                msg_sl = await _sl._call_model(9999, "m:1b", [{"role": "user", "content": "read"}])
        check("subagent_lite._call_model retries 503 via post_chat_json cleanly",
              st_sl["n"] == 2 and msg_sl.get("content") == "summary")
    finally:
        await c_sl.aclose()

    # stream_chat_chunks non-retryable 400 raises clear RuntimeError
    c_err, _ = _mk_client([
        httpx.Response(400, text="bad payload", request=httpx.Request("POST", req_url)),
    ])
    try:
        stream_err = ""
        try:
            async for _ in T.stream_chat_chunks(9999, {"messages": [], "stream": True},
                                                max_attempts=3, client=c_err, log_label="VA"):
                pass
        except RuntimeError as exc:
            stream_err = str(exc)
        check("stream_chat_chunks surfaces HTTP 400 clearly", "VA HTTP 400" in stream_err)
    finally:
        await c_err.aclose()


asyncio.run(_test_caller_wrappers())


# ── 5. Caller integration checks (no bespoke raw HTTP duplicated loops) ──────
for rel, needle in (
    ("core/fix_agent.py", "post_chat_json"),
    ("core/subagent_lite.py", "post_chat_json"),
    ("core/direct_runner.py", "post_chat_json"),
    ("core/duo_runner.py", "post_chat_json"),
    ("core/pipeline_runner.py", "stream_chat_chunks"),
    ("core/tool_loop.py", "iter_tool_loop_post"),
    ("core/agentic_tool_loop.py", "core.llm_transport"),
):
    src = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    check(f"{rel} wired to core.llm_transport ({needle})", needle in src)

# Architectural guard: Stufe B outer loops stay cleanly separate
check("ToolLoop.run outer loop untouched (no duo runner merge)",
      "run_code_duo" not in (ROOT / "core" / "tool_loop.py").read_text(encoding="utf-8"))


sys.exit(h.finish("llm_transport"))
