# -*- coding: utf-8 -*-
"""Shared HTTP transport, retry, recovery, SSE parsing, and usage helpers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

from core.duo_helpers import RE_THINK_CLEANUP as _RE_THINK_CLEANUP
from utils.messages import normalize_system_messages

_log = logging.getLogger("hivemind.llm_transport")

OVERFLOW_ERROR_MARKERS = (
    "exceed",
    "context",
    "too many tokens",
    "context window",
    "n_predict",
)
_TRANSIENT_HTTP_STATUSES = frozenset((408, 429, 500, 502, 503, 504))
_HTTP_SHORT_CAP_STATUSES = frozenset((500, 502, 504))


def is_context_overflow_body(body: str) -> bool:
    """Return True when an error body looks like a context-window overflow."""
    if not body:
        return False
    low = body.lower()
    return any(marker in low for marker in OVERFLOW_ERROR_MARKERS)


def retry_backoff(attempt: int, status_code: int = 0) -> float:
    """Delegate retry delay calculation to backend.llama_client when available."""
    try:
        from backend.llama_client import _retry_backoff as _impl
        return float(_impl(attempt, status_code))
    except ImportError as exc:
        _log.debug("Fallback retry_backoff due to ImportError: %s", exc)
        if status_code == 503:
            return min(2.5, 0.5 * (attempt + 1))
        return min(1.5, 0.25 * (attempt + 1))


def should_retry_http_status(
    status_code: int,
    body: str,
    attempt: int,
    max_attempts: int = 45,
) -> bool:
    """Apply unified F3 HTTP retry policy (reusing backend.llama_client)."""
    try:
        from backend.llama_client import _should_retry_http_status as _impl
        return bool(_impl(status_code, body, attempt, max_attempts))
    except ImportError as exc:
        _log.debug("Fallback F3 status policy due to ImportError: %s", exc)
        if status_code not in _TRANSIENT_HTTP_STATUSES:
            return False
        if attempt >= max_attempts - 1:
            return False
        if status_code in _HTTP_SHORT_CAP_STATUSES:
            if is_context_overflow_body(body):
                return False
            return attempt < 2
        return True


def build_chat_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/v1/chat/completions"


def build_health_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/health"


def normalize_chat_payload(payload: dict) -> dict:
    """Normalize payload messages once before POST without mutating caller dict."""
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        return {**payload, "messages": normalize_system_messages(payload["messages"])}
    return payload


def disable_thinking_in_payload(
    payload: dict,
    *,
    max_tokens: int | None = None,
) -> dict:
    """Return a copy of payload with thinking disabled via kwargs and fields."""
    out = {
        **payload,
        "thinking": False,
        "thinking_budget": 0,
    }
    if max_tokens is not None and int(max_tokens) > 0:
        out["max_tokens"] = int(max_tokens)
    ctk = dict(out.get("chat_template_kwargs") or {})
    ctk["enable_thinking"] = False
    ctk.pop("preserve_thinking", None)
    out["chat_template_kwargs"] = ctk
    return out


def strip_payload_extras(payload: dict) -> dict:
    """Return a copy with non-essential sampler/cache extras stripped on 500 fallback."""
    out = dict(payload)
    out.pop("cache_prompt", None)
    out.pop("min_p", None)
    out["thinking"] = False
    out.pop("thinking_budget", None)
    ctk = dict(out.get("chat_template_kwargs") or {})
    ctk["enable_thinking"] = False
    ctk.pop("preserve_thinking", None)
    out["chat_template_kwargs"] = ctk
    return out


def apply_tool_call_grammar(
    payload: dict,
    grammar_compatible_fn: Callable[[dict], bool],
    build_grammar_fn: Callable[[list[str]], str | None],
) -> tuple[dict, str]:
    """Attach GBNF tool-call grammar if compatible. Returns (payload, status_kind)."""
    if not grammar_compatible_fn(payload):
        return payload, "skipped_tools_active"
    tool_names = [
        t.get("function", {}).get("name", "")
        for t in (payload.get("tools") or [])
        if isinstance(t, dict)
    ]
    grammar = build_grammar_fn([n for n in tool_names if n])
    if grammar:
        return {**payload, "grammar": grammar}, "applied"
    return payload, "empty"


def parse_usage(
    usage_raw: dict | None,
    *,
    gen_ms: int | None = None,
    decode_tps: float | None = None,
) -> dict | None:
    """Normalize OpenAI/llama-server usage dictionary when prompt or completion tokens > 0."""
    if not isinstance(usage_raw, dict):
        return None
    _ct = int(usage_raw.get("completion_tokens") or 0)
    _pt = int(usage_raw.get("prompt_tokens") or 0)
    if _ct <= 0 and _pt <= 0:
        return None
    details = usage_raw.get("prompt_tokens_details")
    cached = 0
    for cand in (
        usage_raw.get("cached_tokens"),
        details.get("cached_tokens") if isinstance(details, dict) else None,
        usage_raw.get("cache_n"),
    ):
        if cand:
            try:
                cached = int(cand)
                break
            except (TypeError, ValueError):
                cached = 0
    out: dict[str, Any] = {
        "completion_tokens": _ct,
        "prompt_tokens": _pt,
        "cached_tokens": cached,
    }
    if gen_ms is not None:
        out["gen_ms"] = int(gen_ms)
    if decode_tps:
        out["decode_tps"] = float(decode_tps)
    return out


def parse_timings(timings_raw: dict | None) -> dict | None:
    """Extract normalized prompt timings from a llama-server chunk or response."""
    if not isinstance(timings_raw, dict) or timings_raw.get("prompt_ms") is None:
        return None
    return {
        "prompt_n": int(timings_raw.get("prompt_n") or 0),
        "prompt_ms": float(timings_raw.get("prompt_ms") or 0.0),
        "prompt_per_second": float(timings_raw.get("prompt_per_second") or 0.0),
    }


def parse_nonstream_chat_response(
    data: dict,
    *,
    strip_content: bool = True,
) -> tuple[dict, str, list[dict]]:
    """Extract (message, content_text, tool_calls) from a non-stream JSON response."""
    if not isinstance(data, dict):
        return {}, "", []
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        msg = first.get("message") or {}
    elif isinstance(data.get("message"), dict):
        msg = data.get("message") or {}
    else:
        msg = {}
    if not isinstance(msg, dict):
        msg = {}
    raw_content = msg.get("content", "") or ""
    content = raw_content.strip() if (strip_content and isinstance(raw_content, str)) else str(raw_content)
    tool_calls = msg.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []
    return msg, content, tool_calls


async def extract_response_error_text(resp: httpx.Response, limit: int = 300) -> str:
    """Safely read an error response body and clip to `limit` characters."""
    try:
        await resp.aread()
    except (httpx.HTTPError, RuntimeError) as exc:
        _log.debug("Failed to read HTTP error body (status=%s): %s", getattr(resp, "status_code", "?"), exc)
    return (getattr(resp, "text", "") or "")[:limit]


async def iter_sse_json(resp: httpx.Response) -> AsyncIterator[dict]:
    """Yield parsed JSON chunk dicts from an OpenAI/llama-server SSE response."""
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            break
        if not raw:
            continue
        try:
            chunk = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            _log.debug("Skipping malformed SSE JSON chunk: %s", exc)
            continue
        if isinstance(chunk, dict):
            yield chunk


async def probe_and_evict_slot(
    model: str,
    *,
    client: httpx.AsyncClient | None = None,
    port: int | None = None,
    check_health: bool = False,
    evict_first: bool = True,
) -> bool:
    """Optionally probe /health on `port` and evict `model` if dead or requested.

    Returns True when /health responded HTTP 200.
    """
    server_alive = False
    if check_health and client is not None and port:
        try:
            hr = await client.get(
                build_health_url(port),
                timeout=httpx.Timeout(connect=2.0, read=2.0, write=1.0, pool=1.0),
            )
            server_alive = hr.status_code == 200
        except (httpx.HTTPError, OSError) as exc:
            _log.debug("Health check failed on port %s: %s", port, exc)

    if evict_first and (not check_health or not server_alive):
        try:
            from backend.llama_server_manager import manager as _lsm
            await _lsm.evict(model)
        except (ImportError, RuntimeError, OSError, httpx.HTTPError) as exc:
            _log.debug("Slot evict failed for %s: %s", model, exc)
    return server_alive


async def recover_slot_port(
    model: str,
    *,
    num_ctx: int = 4096,
    vision: bool = False,
    n_parallel: int = 1,
    evict_first: bool = True,
    client: httpx.AsyncClient | None = None,
    port: int | None = None,
    check_health: bool = False,
    crash_sleep_s: float = 0.0,
) -> tuple[int, bool]:
    """Probe optional /health, evict stale slot if dead/requested, and ensure_loaded."""
    server_alive = await probe_and_evict_slot(
        model,
        client=client,
        port=port,
        check_health=check_health,
        evict_first=evict_first,
    )
    if crash_sleep_s > 0:
        await asyncio.sleep(crash_sleep_s)

    from backend.llama_server_manager import manager as _lsm

    new_port = await _lsm.ensure_loaded(
        model,
        num_ctx=int(num_ctx or 4096),
        n_parallel=int(n_parallel or 1),
        vision=bool(vision),
    )
    return int(new_port), server_alive


@dataclass
class ChatJsonResponse:
    """Normalized non-stream /v1/chat/completions response with telemetry."""

    message: dict
    content_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict | None = None
    timings: dict | None = None
    status_code: int = 200
    attempts: int = 1
    raw: dict = field(default_factory=dict)


def _error_response_message(status_code: int, body: str) -> str:
    clipped = (body or "")[:200]
    return f"HTTP {status_code}: {clipped}" if clipped else f"HTTP {status_code}"


async def post_chat_json(
    port: int,
    payload: dict,
    *,
    timeout: httpx.Timeout | None = None,
    max_attempts: int = 1,
    retry_statuses: tuple[int, ...] = (408, 429, 500, 502, 503, 504),
    retry_sleep_s: float | None = None,
    connect_sleep_s: float = 3.0,
    use_f3_policy: bool = True,
    client: httpx.AsyncClient | None = None,
    normalize_system: bool = False,
    raise_on_status: bool = True,
    log_label: str = "chat_json",
) -> ChatJsonResponse:
    """Send a non-stream /v1/chat/completions JSON POST with unified retry & error handling."""
    eff_timeout = timeout or httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=10.0)
    eff_attempts = max(1, int(max_attempts or 1))
    send_payload = normalize_chat_payload(payload) if normalize_system else payload
    url = build_chat_url(port)

    async def _execute(http_client: httpx.AsyncClient) -> ChatJsonResponse:
        last_status = 0
        last_body = ""
        for attempt in range(eff_attempts):
            t0 = time.monotonic()
            try:
                resp = await http_client.post(url, json=send_payload, timeout=eff_timeout)
                last_status = resp.status_code
                last_body = resp.text[:2000] if resp.status_code >= 400 else ""
                if resp.status_code in retry_statuses and attempt < eff_attempts - 1:
                    should_retry = (
                        should_retry_http_status(resp.status_code, last_body, attempt, eff_attempts)
                        if use_f3_policy
                        else True
                    )
                    if should_retry:
                        delay = (
                            float(retry_sleep_s)
                            if retry_sleep_s is not None
                            else retry_backoff(attempt, resp.status_code)
                        )
                        _log.debug(
                            "[%s] retryable HTTP %d (attempt %d/%d) — sleeping %.2fs",
                            log_label, resp.status_code, attempt + 1, eff_attempts, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                if resp.status_code >= 400:
                    if raise_on_status:
                        resp.raise_for_status()
                    raise RuntimeError(
                        f"{log_label} {_error_response_message(resp.status_code, last_body)}"
                    )
                data = resp.json()
                msg, content_text, tool_calls = parse_nonstream_chat_response(data)
                gen_ms = int((time.monotonic() - t0) * 1000)
                usage = parse_usage(data.get("usage") if isinstance(data, dict) else None, gen_ms=gen_ms)
                timings = parse_timings(data.get("timings") if isinstance(data, dict) else None)
                return ChatJsonResponse(
                    message=msg,
                    content_text=content_text,
                    tool_calls=tool_calls,
                    usage=usage,
                    timings=timings,
                    status_code=resp.status_code,
                    attempts=attempt + 1,
                    raw=data if isinstance(data, dict) else {},
                )
            except httpx.ConnectError as exc:
                _log.debug("[%s] ConnectError on port %s (attempt %d/%d): %s",
                           log_label, port, attempt + 1, eff_attempts, exc)
                if attempt < eff_attempts - 1:
                    await asyncio.sleep(float(connect_sleep_s))
                    continue
                raise
            except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                _log.debug("[%s] transient stream/read error on port %s (attempt %d/%d): %s",
                           log_label, port, attempt + 1, eff_attempts, exc)
                if attempt < eff_attempts - 1:
                    await asyncio.sleep(retry_backoff(attempt))
                    continue
                raise
        raise RuntimeError(
            f"{log_label} POST failed after {eff_attempts} attempts"
            + (f" ({_error_response_message(last_status, last_body)})" if last_status else "")
        )

    if client is not None:
        return await _execute(client)
    async with httpx.AsyncClient(timeout=eff_timeout) as owned_client:
        return await _execute(owned_client)


async def stream_chat_chunks(
    port: int,
    payload: dict,
    *,
    timeout: httpx.Timeout | None = None,
    max_attempts: int = 3,
    connect_sleep_s: float = 2.0,
    client: httpx.AsyncClient | None = None,
    normalize_system: bool = False,
    log_label: str = "chat_stream",
) -> AsyncIterator[dict]:
    """Stream SSE JSON chunks from /v1/chat/completions with F3 retry prior to first chunk."""
    eff_timeout = timeout or httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=5.0)
    eff_attempts = max(1, int(max_attempts or 1))
    send_payload = normalize_chat_payload(payload) if normalize_system else payload
    url = build_chat_url(port)

    async def _open_and_yield(http_client: httpx.AsyncClient) -> AsyncIterator[dict]:
        for attempt in range(eff_attempts):
            partial = False
            try:
                async with http_client.stream("POST", url, json=send_payload, timeout=eff_timeout) as resp:
                    if resp.status_code >= 400:
                        err_body = await extract_response_error_text(resp, limit=2000)
                        if should_retry_http_status(resp.status_code, err_body, attempt, eff_attempts):
                            delay = retry_backoff(attempt, resp.status_code)
                            _log.debug(
                                "[%s] retryable stream HTTP %d (attempt %d/%d) — sleeping %.2fs",
                                log_label, resp.status_code, attempt + 1, eff_attempts, delay,
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise RuntimeError(
                            f"{log_label} HTTP {resp.status_code}: {err_body[:200]}"
                        )
                    async for chunk in iter_sse_json(resp):
                        partial = True
                        yield chunk
                    return
            except httpx.ConnectError as exc:
                if partial or attempt >= eff_attempts - 1:
                    raise
                _log.debug("[%s] ConnectError before stream on port %s (attempt %d/%d): %s",
                           log_label, port, attempt + 1, eff_attempts, exc)
                await asyncio.sleep(float(connect_sleep_s))
            except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                if partial or attempt >= eff_attempts - 1:
                    raise
                _log.debug("[%s] read interruption before stream on port %s (attempt %d/%d): %s",
                           log_label, port, attempt + 1, eff_attempts, exc)
                await asyncio.sleep(retry_backoff(attempt))

    if client is not None:
        async for chunk in _open_and_yield(client):
            yield chunk
        return
    async with httpx.AsyncClient(timeout=eff_timeout) as owned_client:
        async for chunk in _open_and_yield(owned_client):
            yield chunk


def _record_tool_loop_usage(loop_obj: Any, usage: dict | None) -> None:
    """Accumulate server prompt/completion/cached token counters onto ToolLoop + state."""
    add_fn = getattr(loop_obj, "_add_usage", None)
    if callable(add_fn):
        add_fn(usage)


async def _handle_tool_loop_http_error(
    loop_obj: Any,
    resp: httpx.Response,
    payload: dict,
    attempt: int,
    *,
    retry_allowed_fn: Callable[[Any, str, int], bool],
    inject_feedback_fn: Callable[..., None],
    build_grammar_fn: Callable[[list[str]], str | None],
    grammar_compatible_fn: Callable[[dict], bool],
    use_gbnf_grammar: bool,
    fdbk_injected: list[bool],
    events_out: list[dict],
) -> tuple[str, dict]:
    """Handle HTTP >= 400 in ToolLoop. Returns (action, updated_payload).

    action is one of: 'retry', 'raise'.
    """
    cfg = loop_obj.cfg
    err_body = await extract_response_error_text(resp, limit=300)

    if resp.status_code in (500, 502, 503, 504):
        low = err_body.lower()
        if resp.status_code == 500 and cfg.retry_on_grammar_crash and "CallExpression" in err_body and attempt < cfg.max_post_attempts - 1:
            if not fdbk_injected[0]:
                inject_feedback_fn(
                    payload["messages"],
                    error_type="parse_error",
                    bad_content="",
                    model=getattr(cfg, "model", ""),
                )
                fdbk_injected[0] = True
            if cfg.thinking:
                cfg.thinking = False
                cfg.thinking_budget = 0
                payload = dict(payload)
                payload.pop("thinking", None)
                payload.pop("thinking_budget", None)
                _ctk = dict(payload.get("chat_template_kwargs") or {})
                _ctk["enable_thinking"] = False
                payload["chat_template_kwargs"] = _ctk
                if use_gbnf_grammar:
                    payload, _ = apply_tool_call_grammar(payload, grammar_compatible_fn, build_grammar_fn)
                events_out.append({"type": "status", "content": "⚠ Grammar-Crash (CallExpression) — retry without thinking…"})
                return "retry", payload
            payload = dict(payload)
            payload.pop("cache_prompt", None)
            payload.pop("min_p", None)
            payload.pop("chat_template_kwargs", None)
            events_out.append({"type": "status", "content": "⚠ Grammar-Crash — stripping extras and retry…"})
            return "retry", payload

        if resp.status_code == 500 and cfg.retry_on_parse_error and "parse" in low and attempt < cfg.max_post_attempts - 1:
            if not fdbk_injected[0]:
                inject_feedback_fn(
                    payload["messages"],
                    error_type="parse_error",
                    bad_content="",
                    model=getattr(cfg, "model", ""),
                )
                fdbk_injected[0] = True
            events_out.append({"type": "status", "content": "⚠ JSON parse error — retry…"})
            return "retry", payload

        if retry_allowed_fn(cfg, "5xx", attempt) and should_retry_http_status(
            resp.status_code, resp.text[:2000], attempt, cfg.max_post_attempts
        ):
            await asyncio.sleep(1.5 + min(1.0, attempt * 0.1))
            events_out.append({"type": "status", "content": f"⏳ Server {resp.status_code}, retry {attempt + 1}/{cfg.max_post_attempts}…"})
            return "retry", payload
        raise RuntimeError(f"Tool-POST HTTP {resp.status_code}: {err_body}")

    if resp.status_code == 404 and retry_allowed_fn(cfg, "404", attempt):
        await asyncio.sleep(2.0)
        events_out.append({"type": "status", "content": f"⚠ HTTP 404, retry {attempt + 1}…"})
        return "retry", payload

    if resp.status_code == 400 and attempt < cfg.max_post_attempts - 1:
        low = err_body.lower()
        if cfg.retry_on_context_overflow and any(k in low for k in ("exceed", "context", "token", "prompt")):
            events_out.append({"type": "status", "content": "⚠ Context overflow — compress and retry…"})
            events_out.append({"type": "ctx_overflow", "content": "compress"})
            return "retry", payload
        if payload.get("grammar") and "grammar" in low:
            cleaned = dict(payload)
            cleaned.pop("grammar", None)
            events_out.append({"type": "status", "content": "⚠ Grammar+tools rejected by server — stripped grammar, retrying…"})
            return "retry", cleaned

    raise RuntimeError(f"Tool-POST HTTP {resp.status_code}: {err_body}")


async def iter_tool_loop_post(
    loop_obj: Any,
    payload: dict,
    *,
    stream: bool,
    retry_allowed_fn: Callable[[Any, str, int], bool],
    grammar_compatible_fn: Callable[[dict], bool],
    build_grammar_fn: Callable[[list[str]], str | None],
    inject_feedback_fn: Callable[..., None],
    parse_sse_delta_fn: Callable[..., tuple[str, str]],
    use_gbnf_grammar: bool = True,
) -> AsyncIterator[dict | tuple[dict, str, list[dict]]]:
    """Execute ToolLoop stream or non-stream POST with unified retry & SSE parsing.

    Yields UI events ('status', 'ctx_overflow', 'token') and concludes with the
    3-tuple (msg, content_text, tool_calls).
    """
    cfg = loop_obj.cfg
    send_payload = normalize_chat_payload(payload)
    port = int(loop_obj._port)
    timeout = httpx.Timeout(connect=10.0, read=cfg.read_timeout_s, write=10.0, pool=5.0)
    fdbk_injected = [False]
    url = build_chat_url(port)

    for attempt in range(max(1, int(cfg.max_post_attempts or 1))):
        partial_stream = False
        try:
            async with loop_obj._client.stream("POST", url, json=send_payload, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    err_events: list[dict] = []
                    action, send_payload = await _handle_tool_loop_http_error(
                        loop_obj,
                        resp,
                        send_payload,
                        attempt,
                        retry_allowed_fn=retry_allowed_fn,
                        inject_feedback_fn=inject_feedback_fn,
                        build_grammar_fn=build_grammar_fn,
                        grammar_compatible_fn=grammar_compatible_fn,
                        use_gbnf_grammar=use_gbnf_grammar,
                        fdbk_injected=fdbk_injected,
                        events_out=err_events,
                    )
                    for ev in err_events:
                        yield ev
                    if action == "retry":
                        continue

                if not stream:
                    await resp.aread()
                    data = resp.json()
                    _record_tool_loop_usage(loop_obj, data.get("usage") if isinstance(data, dict) else None)
                    msg, content_text, tcs = parse_nonstream_chat_response(data, strip_content=False)
                    yield (msg, content_text, tcs)
                    return

                content_parts: list[str] = []
                tool_calls_acc: dict[int, dict] = {}
                async for chunk in iter_sse_json(resp):
                    _record_tool_loop_usage(loop_obj, chunk.get("usage"))
                    choices = chunk.get("choices") or [{}]
                    delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
                    if not isinstance(delta, dict):
                        continue
                    ct, _ = parse_sse_delta_fn(delta, tool_calls_acc=tool_calls_acc)
                    if ct:
                        content_parts.append(ct)
                        yield {"type": "token", "content": ct}
                        partial_stream = True

                tcs_sorted = [tool_calls_acc[k] for k in sorted(tool_calls_acc.keys())]
                content = "".join(content_parts).strip()
                if content and "<think" in content:
                    content = _RE_THINK_CLEANUP.sub("", content).strip()
                msg = {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tcs_sorted,
                }
                yield (msg, content or "", tcs_sorted)
                return

        except httpx.ConnectError:
            if retry_allowed_fn(cfg, "connect", attempt):
                await asyncio.sleep(5.0)
                yield {"type": "status", "content": f"⟳ Connection error, retry {attempt + 2}…"}
                continue
            raise
        except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError):
            if partial_stream:
                raise
            if retry_allowed_fn(cfg, "read", attempt):
                await asyncio.sleep(3.0)
                yield {"type": "status", "content": f"⚠ Read timeout, retry {attempt + 1}…"}
                continue
            raise

    raise RuntimeError(f"Tool-POST: all {cfg.max_post_attempts} attempts failed")


PostJsonFn = Callable[[int, dict], Awaitable[ChatJsonResponse]]
