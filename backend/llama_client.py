


from __future__ import annotations

import asyncio
import httpx
import json
import logging
import time
import uuid
from typing import AsyncIterator, Optional

from .llama_server_manager import manager
from .llama_config import CONTEXT_SIZE_DEFAULT
from .llama_models import resolve_model_path


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


        return any("images" in m and m.get("images") for m in messages)

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
                                 thinking=think, thinking_budget=thinking_budget)
        _vision = self._has_images(messages)
        port = await manager.ensure_loaded(model, num_ctx=ctx, vision=_vision)
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
                if r.status_code in _TRANSIENT_HTTP_STATUSES and attempt < max_attempts - 1:
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
                return data["choices"][0]["message"]["content"]
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
                if e.response.status_code == 404 and attempt < max_attempts - 1:
                    backoff = _retry_backoff(attempt)
                    self._log.warning(
                        "req=%s chat HTTP 404 -> recover model=%s attempt=%d/%d port=%s backoff=%.2fs",
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
                if e.response.status_code in _TRANSIENT_HTTP_STATUSES and attempt < max_attempts - 1:
                    backoff = _retry_backoff(attempt, e.response.status_code)
                    self._log.warning(
                        "req=%s chat HTTP transient=%s model=%s attempt=%d/%d port=%s backoff=%.2fs",
                        req_id,
                        e.response.status_code,
                        model,
                        attempt + 1,
                        max_attempts,
                        port,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise LlamaError(f"[{req_id}] HTTP {e.response.status_code} von llama-server")
        raise LlamaError(f"[{req_id}] llama-server chat failure for '{model}' after retry")

    async def chat_stream(self, model: str, messages: list, temperature: float = 0.3,
                          max_tokens: int = 600, ctx: int = CONTEXT_SIZE_DEFAULT,
                          think: bool | None = None, no_cache: bool = False,
                          thinking_budget: int | None = None,
                          split_thinking: bool = False) -> AsyncIterator[str | tuple[str, str]]:


        req_id = self._req_id("stream")
        t0 = time.perf_counter()
        payload = _build_payload(messages, temperature, max_tokens, stream=True,
                                 cache_prompt=not no_cache, thinking=think,
                                 thinking_budget=thinking_budget)
        _vision = self._has_images(messages)
        port = await manager.ensure_loaded(model, num_ctx=ctx, vision=_vision)
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
                        if r.status_code in _TRANSIENT_HTTP_STATUSES and attempt < max_attempts - 1:
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
                if e.response.status_code == 404 and attempt < max_attempts - 1:
                    backoff = _retry_backoff(attempt)
                    self._log.warning(
                        "req=%s stream HTTP 404 -> recover model=%s attempt=%d/%d port=%s backoff=%.2fs",
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
                if e.response.status_code in _TRANSIENT_HTTP_STATUSES and attempt < max_attempts - 1:
                    backoff = _retry_backoff(attempt, e.response.status_code)
                    self._log.warning(
                        "req=%s stream HTTP transient=%s model=%s attempt=%d/%d port=%s backoff=%.2fs",
                        req_id,
                        e.response.status_code,
                        model,
                        attempt + 1,
                        max_attempts,
                        port,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise LlamaError(f"[{req_id}] HTTP {e.response.status_code} von llama-server")
        raise LlamaError(f"[{req_id}] llama-server stream failure for '{model}' after retry")

    async def warmup(self, model: str, num_ctx: Optional[int] = None):


        try:
            await manager.load(
                model,
                keep_alive_seconds=600,
                num_ctx=num_ctx or CONTEXT_SIZE_DEFAULT,
                pin=False,
            )
        except Exception:
            pass

    async def close(self):
        """Wird beim App-Shutdown aufgerufen."""
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


def _build_payload(messages: list, temperature: float, max_tokens: int,
                   stream: bool, cache_prompt: bool = True,
                   thinking: bool | None = None,
                   thinking_budget: int | None = None) -> dict:


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
    if thinking is not None:
        payload["thinking"] = thinking
        payload.setdefault("chat_template_kwargs", {})["enable_thinking"] = bool(thinking)
        if not thinking:
            payload["thinking_budget"] = 0
    if thinking_budget is not None:
        payload["thinking_budget"] = thinking_budget
    return payload


