


from __future__ import annotations

import asyncio
import httpx
import json
import logging
import time
import uuid
from typing import AsyncIterator

from .llama_server_manager import manager
from .llama_config import CONTEXT_SIZE_DEFAULT


class LlamaError(Exception):
    pass

OllamaError = LlamaError

_TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_TRANSIENT_HTTP_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
    httpx.WriteError,
    httpx.PoolTimeout,
)


def _retry_backoff(attempt: int, status_code: int = 0) -> float:
    if status_code == 503:
        return min(2.5, 0.5 * (attempt + 1))
    return min(1.5, 0.25 * (attempt + 1))


# RETRY-POLICY (F3, deep audit 2026-11-07): a deterministic 500 (context
# overflow, chat-template/grammar error) used to be retried 45x with the
# FULL prompt - hours of repeated 35B prefill while the run looked alive.
# 408/429/503 keep the long retry (timeouts, rate limits, model load and
# eviction are genuinely transient); generic 500/502/504 get a short cap
# and an overflow-shaped body never retries at all.
_OVERFLOW_MARKERS = ("exceed", "context", "too many tokens",
                     "context window", "n_predict")
_HTTP_SERVER_ERRORS = {500, 502, 504}


def _is_context_overflow_body(body: str) -> bool:
    """Heuristic: does the error body look like a context-overflow rejection?"""
    if not body:
        return False
    low = body.lower()
    return any(marker in low for marker in _OVERFLOW_MARKERS)


def _should_retry_http_status(status_code: int, body: str, attempt: int,
                              max_attempts: int = 45) -> bool:
    """True only when another attempt can plausibly succeed."""
    if status_code not in _TRANSIENT_HTTP_STATUSES:
        return False
    if attempt >= max_attempts - 1:
        return False
    if status_code in _HTTP_SERVER_ERRORS:
        if _is_context_overflow_body(body):
            return False
        # plain 5xx: 3 total attempts (attempt 0/1 may fire again,
        # attempt 2 is the last one) - enough for a real blip, harmless
        # for a deterministic error.
        return attempt < 2
    # 408/429/503: unchanged long retry
    return True


async def tokenize_count(port: int, text: str, timeout_s: float = 5.0) -> int | None:
    """Real token count for `text` from the running llama-server (/tokenize).

    Returns None on any problem (no slot, old build without the endpoint,
    timeout, malformed answer) - callers must fall back to their estimate.
    One short local call, no retry: this is instrumentation, not a
    correctness path."""
    if not port or not text:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=timeout_s, write=2.0, pool=2.0)
        ) as c:
            r = await c.post(f"http://127.0.0.1:{int(port)}/tokenize",
                             json={"content": text})
            if r.status_code != 200:
                return None
            data = r.json() or {}
            toks = data.get("tokens")
            if isinstance(toks, list) and toks:
                return len(toks)
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OSError) as _e:
        logging.getLogger("hivemind.llama_client").debug(
            "tokenize probe on port %s failed: %s", port, _e)
    return None


class LlamaClient:


    def __init__(self):
        # Localhost-Calls: spart TCP-Handshake (~0.5-2ms) pro Agent-Call.
        # limits: max 4 gleichzeitige Connections (MAX_SLOTS=2, je 1 chat+health reicht).
        self._client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=5.0),
        )
        self._log = logging.getLogger("hivemind.llama_client")

    @staticmethod
    def _req_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _has_images(messages: list) -> bool:
        # RAW `images` key OR OpenAI-style image_url parts. Direct steers and
        # compressed histories carry image_url parts; checking only the raw
        # key let a projector-less slot receive them (POST 500).
        for _m in messages:
            if not isinstance(_m, dict):
                continue
            if _m.get("images"):
                return True
            _content = _m.get("content")
            if isinstance(_content, list) and any(
                    isinstance(_p, dict) and _p.get("type") == "image_url"
                    for _p in _content):
                return True
        return False

    async def _recover_model_port(self, model: str, ctx: int, old_port: int | None = None,
                                  vision: bool = False) -> int:
        """Best-effort recovery for stale/broken model slots, then return a fresh port."""
        try:
            await manager.evict(model)
        except Exception:
            pass
        new_port = await manager.ensure_loaded(model, num_ctx=ctx, vision=vision)
        self._log.warning(
            "Recovered llama-server slot for model=%s old_port=%s new_port=%s vision=%s",
            model,
            old_port,
            new_port,
            vision,
        )
        return int(new_port)

    async def chat(self, model: str, messages: list, temperature: float = 0.3,
                   max_tokens: int = 600, ctx: int = CONTEXT_SIZE_DEFAULT,
                   think: bool | None = None,
                   thinking_budget: int | None = None) -> str:
        """Non-streaming Chat. Entspricht OllamaClient.chat()."""
        req_id = self._req_id("chat")
        t0 = time.perf_counter()
        payload = _build_payload(messages, temperature, max_tokens, stream=False,
                                 thinking=think, thinking_budget=thinking_budget,
                                 sampler=SIDE_CALL_SAMPLING)
        _vision = self._has_images(messages)
        port = await manager.ensure_loaded(model, num_ctx=ctx, vision=_vision)
        # SAMPLER-PROOF (2026-10-30): log what really goes on the wire - the
        # only way to show top_k/min_p/presence_penalty arrive at the server.
        self._log.info(
            "[SAMPLER] kind=side model=%s port=%s temp=%s top_p=%s top_k=%s "
            "min_p=%s presence=%s thinking=%s",
            model, port, payload.get("temperature"), payload.get("top_p"),
            payload.get("top_k"), payload.get("min_p"),
            payload.get("presence_penalty"),
            (payload.get("chat_template_kwargs") or {}).get("enable_thinking"),
        )
        max_attempts = 45
        for attempt in range(max_attempts):
            try:
                r = await self._client.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json=payload,
                    timeout=httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0),
                )
                if r.status_code >= 400:
                    self._log.error(
                        "req=%s llama-server %s fuer '%s' (chat) | body: %s",
                        req_id,
                        r.status_code,
                        model,
                        r.text[:400],
                    )
                if r.status_code == 404 and attempt < max_attempts - 1:
                    backoff = _retry_backoff(attempt)
                    self._log.warning(
                        "req=%s chat 404 -> recover model=%s attempt=%d/%d port=%s backoff=%.2fs",
                        req_id,
                        model,
                        attempt + 1,
                        max_attempts,
                        port,
                        backoff,
                    )
                    port = await self._recover_model_port(model, ctx, old_port=port, vision=_vision)
                    await asyncio.sleep(backoff)
                    continue
                if _should_retry_http_status(
                        r.status_code, r.text[:2000], attempt, max_attempts):
                    backoff = _retry_backoff(attempt, r.status_code)
                    self._log.warning(
                        "req=%s chat transient status=%s model=%s attempt=%d/%d port=%s backoff=%.2fs",
                        req_id,
                        r.status_code,
                        model,
                        attempt + 1,
                        max_attempts,
                        port,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                r.raise_for_status()
                data = r.json()
                self._log.debug(
                    "req=%s chat success model=%s port=%s elapsed=%.2fs attempts=%d",
                    req_id,
                    model,
                    port,
                    time.perf_counter() - t0,
                    attempt + 1,
                )
                # PERF-TRACE (2026-10-30): one grep-able line per side call -
                # without it the compression/summary calls were invisible in
                # the cache-loss analysis.
                try:
                    from utils.perf_log import perf_line as _perf_line
                    self._log.info(_perf_line(
                        kind="side", model=model, port=str(port),
                        usage=data.get("usage") or {},
                        timings=data.get("timings") or {}))
                except (ImportError, AttributeError, TypeError) as _perf_err:
                    self._log.debug("perf line skipped: %s", _perf_err)
                # EMPTY-CONTENT-POLICY (2026-10-30, revised): some models
                # ignore chat_template_kwargs.enable_thinking and spend the
                # whole max_tokens budget inside the think block, so `content`
                # comes back empty (measured: LFM2.5 2.6B - 800/800 tokens,
                # 0 chars content, 125+ chars reasoning).
                # POLICY: the reasoning text is NOT substituted. Titles,
                # memory writes and insights persist what they get, and a
                # think-block fragment must never end up as stored knowledge.
                # Log it and return "" - the caller skips the result.
                _msg = data["choices"][0]["message"]
                _content = _msg.get("content") or ""
                if not _content.strip():
                    _reason = (_msg.get("reasoning_content")
                               or _msg.get("reasoning") or "").strip()
                    self._log.warning(
                        "[SIDE-SKIP] model=%s returned empty content "
                        "(reasoning_chars=%d, thinking not suppressed) - result skipped",
                        model, len(_reason))
                    return ""
                return _content
            except _TRANSIENT_HTTP_EXCEPTIONS as e:
                if attempt < max_attempts - 1:
                    backoff = _retry_backoff(attempt)
                    self._log.warning(
                        "req=%s chat transient error=%s model=%s attempt=%d/%d port=%s backoff=%.2fs",
                        req_id,
                        type(e).__name__,
                        model,
                        attempt + 1,
                        max_attempts,
                        port,
                        backoff,
                    )
                    if isinstance(e, httpx.ConnectError):
                        port = await self._recover_model_port(model, ctx, old_port=port, vision=_vision)
                    await asyncio.sleep(backoff)
                    continue
                raise LlamaError(f"[{req_id}] llama-server for '{model}' unreachable (port {port})")
            except httpx.HTTPStatusError as e:
                # the 404/transient retries already ran pre-raise_for_status();
                # every path reaching here is final
                raise LlamaError(f"[{req_id}] HTTP {e.response.status_code} von llama-server")
        raise LlamaError(f"[{req_id}] llama-server chat failure for '{model}' after retry")

    async def chat_stream(self, model: str, messages: list, temperature: float = 0.3,
                          max_tokens: int = 600, ctx: int = CONTEXT_SIZE_DEFAULT,
                          think: bool | None = None, no_cache: bool = False,
                          thinking_budget: int | None = None,
                          split_thinking: bool = False,
                          usage_out: dict | None = None) -> AsyncIterator[str | tuple[str, str]]:


        req_id = self._req_id("stream")
        t0 = time.perf_counter()
        _vision = self._has_images(messages)
        port = await manager.ensure_loaded(model, num_ctx=ctx, vision=_vision)
        # VISION TRUTH (2026-10-04): direct mode used to trust the
        # profile flag + the requested load flag only. If the loaded server
        # really runs WITHOUT a projector (/props vision:false), sending the
        # image would 400 the whole request - degrade to text-only with a
        # loud log line instead (the images_in_request trace then shows 0).
        if _vision:
            try:
                _slot = manager.get_slot_by_port(port)
                _va = getattr(_slot, "vision_active", None) if _slot is not None else None
                if _va is False:
                    messages = [dict(_m) if isinstance(_m, dict) else _m for _m in messages]
                    for _m in messages:
                        if isinstance(_m, dict):
                            _m.pop("images", None)
                            if isinstance(_m.get("content"), list):
                                _m["content"] = [
                                    _p for _p in _m["content"]
                                    if not (isinstance(_p, dict) and _p.get("type") == "image_url")
                                ]
                    self._log.warning(
                        "req=%s model='%s': server runs WITHOUT vision projector "
                        "(vision_active=False) - images dropped, request goes text-only",
                        req_id, model)
            except (ImportError, AttributeError, TypeError, ValueError) as _vt_err:
                self._log.debug("req=%s vision-truth check skipped: %s", req_id, _vt_err)
        payload = _build_payload(messages, temperature, max_tokens, stream=True,
                                 cache_prompt=not no_cache, thinking=think,
                                 thinking_budget=thinking_budget)
        # USAGE-REPORT (2026-10-20): nur wenn der Aufrufer es anfordert, damit
        # bestehende Pfade (direct, duo) unveraendert bleiben.
        if usage_out is not None:
            payload["stream_options"] = {"include_usage": True}
        max_attempts = 45
        _yielded_any = False
        for attempt in range(max_attempts):
            try:
                async with self._client.stream(
                    "POST",
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json=payload,
                    timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=5.0),
                ) as r:
                    if r.status_code >= 400:
                        body = await r.aread()
                        self._log.error(
                            "req=%s llama-server %s fuer '%s' | payload-keys: %s | msg-roles: %s | body: %s",
                            req_id,
                            r.status_code,
                            model,
                            list(payload.keys()),
                            [m.get("role") for m in payload.get("messages", [])],
                            body.decode(errors="replace")[:400],
                        )
                        if r.status_code == 404 and attempt < max_attempts - 1:
                            backoff = _retry_backoff(attempt)
                            self._log.warning(
                                "req=%s stream 404 -> recover model=%s attempt=%d/%d port=%s backoff=%.2fs",
                                req_id,
                                model,
                                attempt + 1,
                                max_attempts,
                                port,
                                backoff,
                            )
                            port = await self._recover_model_port(model, ctx, old_port=port, vision=_vision)
                            await asyncio.sleep(backoff)
                            continue
                        if _should_retry_http_status(
                                r.status_code,
                                body.decode(errors="replace")[:2000],
                                attempt, max_attempts):
                            backoff = _retry_backoff(attempt, r.status_code)
                            self._log.warning(
                                "req=%s stream transient status=%s model=%s attempt=%d/%d port=%s backoff=%.2fs",
                                req_id,
                                r.status_code,
                                model,
                                attempt + 1,
                                max_attempts,
                                port,
                                backoff,
                            )
                            await asyncio.sleep(backoff)
                            continue
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        line = line.strip()
                        if not line or line == "data: [DONE]":
                            continue
                        if line.startswith("data: "):
                            line = line[6:]
                        try:
                            chunk = json.loads(line)
                            if usage_out is not None:
                                _u = chunk.get("usage")
                                if isinstance(_u, dict) and _u:
                                    usage_out.update(_u)
                                _t = chunk.get("timings")
                                if isinstance(_t, dict) and _t:
                                    usage_out["timings"] = _t
                            delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                            _content = delta.get("content", "")
                            # AGENT-THINKING: Reasoning-Felder getrennt erfassen
                            _thinking = ""
                            for _k in ("thinking", "reasoning_content", "reasoning"):
                                _thinking = delta.get(_k, "") or ""
                                if _thinking:
                                    break
                            if split_thinking:
                                if think is False:
                                    _thinking = ""
                                if (_content and _content != "\0") or (_thinking and _thinking != "\0"):
                                    _yielded_any = True
                                    yield _content, _thinking
                            else:
                                # streamen. reasoning_content (englische Chain-of-Thought)
                                if think is False:
                                    token = _content
                                else:
                                    token = _content or _thinking
                                if token and token != "\0":
                                    _yielded_any = True
                                    yield token
                        except json.JSONDecodeError:
                            continue
                    self._log.debug(
                        "req=%s stream success model=%s port=%s elapsed=%.2fs attempts=%d",
                        req_id,
                        model,
                        port,
                        time.perf_counter() - t0,
                        attempt + 1,
                    )
                    return
            except _TRANSIENT_HTTP_EXCEPTIONS as e:
                if _yielded_any:
                    raise LlamaError(
                        f"[{req_id}] stream interrupted after partial output ({type(e).__name__})"
                    ) from e
                if attempt < max_attempts - 1:
                    backoff = _retry_backoff(attempt)
                    self._log.warning(
                        "req=%s stream transient error=%s model=%s attempt=%d/%d port=%s backoff=%.2fs",
                        req_id,
                        type(e).__name__,
                        model,
                        attempt + 1,
                        max_attempts,
                        port,
                        backoff,
                    )
                    if isinstance(e, httpx.ConnectError):
                        port = await self._recover_model_port(model, ctx, old_port=port, vision=_vision)
                    await asyncio.sleep(backoff)
                    continue
                raise LlamaError(f"[{req_id}] llama-server for '{model}' unreachable (port {port})")
            except httpx.HTTPStatusError as e:
                # the 404/transient retries already ran pre-raise_for_status();
                # every path reaching here is final
                raise LlamaError(f"[{req_id}] HTTP {e.response.status_code} von llama-server")
        raise LlamaError(f"[{req_id}] llama-server stream failure for '{model}' after retry")

    async def close(self):
        """Called on app shutdown."""
        try:
            await self._client.aclose()
        except Exception:
            pass
        await manager.shutdown()


# ── Payload Builder ───────────────────────────────────────────────────────────

def _convert_messages(messages: list) -> list:


    if not any("images" in m for m in messages):
        return messages

    result = []
    for msg in messages:
        images = msg.get("images")
        if not images:
            if "images" in msg:
                result.append({k: v for k, v in msg.items() if k != "images"})
            else:
                result.append(msg)
            continue

        content_parts: list = []
        for b64 in images:
            if not b64.startswith("data:"):
                b64 = f"data:image/jpeg;base64,{b64}"
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": b64}
            })

        text = msg.get("content", "")
        if text:
            content_parts.append({"type": "text", "text": text})

        result.append({"role": msg["role"], "content": content_parts})
    return result


# SIDE-CALL SAMPLER (2026-10-30, user decision): summaries, titles and
# memory calls are not creative work - the model cards ask for a narrow
# distribution (top_p 0.8) plus a mild repetition brake. These keys are sent
# EXPLICITLY (llama-server defaults would otherwise apply: top_k 40,
# min_p 0.05, presence 0) and logged once per request as [SAMPLER].
SIDE_CALL_SAMPLING = {
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
}


def _build_payload(messages: list, temperature: float, max_tokens: int,
                   stream: bool, cache_prompt: bool = True,
                   thinking: bool | None = None,
                   thinking_budget: int | None = None,
                   sampler: dict | None = None) -> dict:


    payload = {
        "messages":          _convert_messages(messages),
        "temperature":       temperature,
        "max_tokens":        max_tokens,
        "stream":            stream,
        "repeat_penalty":    1.25,
        "repeat_last_n":     128,
        "frequency_penalty": 0.1,
        "cache_prompt":      cache_prompt,
    }
    # SIDE-CALL SAMPLER, OPT-IN ONLY (2026-10-30, audit fix): this used to
    # be applied unconditionally, which silently rewrote the sampler of the
    # MAIN chat path (chat_stream -> _build_payload) to temp 0.7 / top_p 0.8
    # / presence 1.5 and overrode the user's temperature. Side calls pass
    # sampler=SIDE_CALL_SAMPLING explicitly; every other caller keeps the
    # sampler it asked for.
    if sampler:
        for _k, _v in sampler.items():
            payload[_k] = _v
    if thinking is not None:
        payload["thinking"] = thinking
        payload.setdefault("chat_template_kwargs", {})["enable_thinking"] = bool(thinking)
        if not thinking:
            payload["thinking_budget"] = 0
    if thinking_budget is not None:
        payload["thinking_budget"] = thinking_budget
    return payload


