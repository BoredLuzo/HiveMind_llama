# -*- coding: utf-8 -*-
"""HTTP retry policy of backend/llama_client (F3, deep audit 2026-11-07).

Live finding: a deterministic 500 (context overflow, chat-template or
grammar error) was retried 45x in chat() AND chat_stream() with the full
prompt - on a 35B model that is hours of repeated prefill while the run
looks alive. New policy:
  - 408/429/503 keep the long retry (model load/eviction, timeouts, limits)
  - generic 500/502/504 are capped at 3 total attempts
  - an overflow-shaped error body never retries at all
  - 404/ConnectError recovery paths are unchanged

Run: python tests/test_llama_client_retry.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

import httpx  # noqa: E402
import backend.llama_client as M  # noqa: E402
from backend.llama_client import LlamaClient, LlamaError  # noqa: E402


# ── 1. _is_context_overflow_body ────────────────────────────────────────────
check("empty body is not overflow", M._is_context_overflow_body("") is False)
check("None-ish body is not overflow", M._is_context_overflow_body(None or "") is False)
check("'exceed' matches",
      M._is_context_overflow_body("the request exceeds the available size"))
check("'context' matches (case-insensitive)",
      M._is_context_overflow_body("Context Window is full"))
check("'too many tokens' matches",
      M._is_context_overflow_body("error: too many tokens"))
check("'n_predict' matches",
      M._is_context_overflow_body('{"error":{"message":"n_predict must be > 0"}}'))
check("plain server error is not overflow",
      M._is_context_overflow_body("internal server error: template crash") is False)


# ── 2. non-transient statuses never retry ───────────────────────────────────
for status in (400, 401, 403, 404, 416, 422):
    check(f"{status} -> False",
          M._should_retry_http_status(status, "anything", 0) is False)
check("416 at high attempt -> False",
      M._should_retry_http_status(416, "", 10) is False)


# ── 3. 408/429/503 keep the long retry ──────────────────────────────────────
check("503 retryable at attempt 0",
      M._should_retry_http_status(503, "", 0) is True)
check("503 retryable at attempt 20 (model load/eviction window)",
      M._should_retry_http_status(503, "", 20) is True)
check("503 retryable at attempt 43",
      M._should_retry_http_status(503, "", 43) is True)
check("429 retryable at attempt 40",
      M._should_retry_http_status(429, "rate limited", 40) is True)
check("408 retryable at attempt 40",
      M._should_retry_http_status(408, "timeout", 40) is True)
check("503 overflow-ish body still retries (long-retry class wins)",
      M._should_retry_http_status(503, "context window exceeded", 20) is True)


# ── 4. plain 500/502/504 cap at 3 total attempts ────────────────────────────
check("plain 500 attempt 0 -> True (2nd try)",
      M._should_retry_http_status(500, "internal error", 0) is True)
check("plain 500 attempt 1 -> True (3rd try)",
      M._should_retry_http_status(500, "internal error", 1) is True)
check("plain 500 attempt 2 -> False (cap reached)",
      M._should_retry_http_status(500, "internal error", 2) is False)
check("plain 500 attempt 3 -> False",
      M._should_retry_http_status(500, "internal error", 3) is False)
check("plain 502 attempt 2 -> False",
      M._should_retry_http_status(502, "bad gateway", 2) is False)
check("plain 504 attempt 2 -> False",
      M._should_retry_http_status(504, "gateway timeout", 2) is False)
check("502 attempt 0 -> True", M._should_retry_http_status(502, "", 0) is True)
check("504 attempt 1 -> True", M._should_retry_http_status(504, "", 1) is True)
check("'template' 500 has no overflow marker -> still caps at 3",
      M._should_retry_http_status(500, "template error: unexpected role", 2) is False)


# ── 5. overflow-shaped 5xx body never retries ───────────────────────────────
check("500 'context window exceeded' -> False immediately",
      M._should_retry_http_status(500, "context window exceeded", 0) is False)
check("500 'exceeds context' JSON body -> False",
      M._should_retry_http_status(500, '{"error":"the prompt exceeds the context"}', 0) is False)
check("502 overflow body -> False", M._should_retry_http_status(502, "too many tokens", 0) is False)
check("504 overflow body -> False", M._should_retry_http_status(504, "n_predict out of range", 0) is False)


# ── 6. max_attempts boundary ────────────────────────────────────────────────
check("503 at attempt 44 (last) -> False",
      M._should_retry_http_status(503, "", 44) is False)
check("500 at attempt 44 -> False",
      M._should_retry_http_status(500, "", 44) is False)
check("408 at attempt 44 -> False",
      M._should_retry_http_status(408, "", 44) is False)
check("custom max_attempts=3: attempt 2 -> False",
      M._should_retry_http_status(503, "", 2, max_attempts=3) is False)
check("custom max_attempts=3: attempt 1 -> True",
      M._should_retry_http_status(503, "", 1, max_attempts=3) is True)
check("custom max_attempts=3: 500 attempt 1 -> True (cap not hit yet)",
      M._should_retry_http_status(500, "", 1, max_attempts=3) is True)


# ── 7. source guards: both call sites rewired, 404 path untouched ──────────
_src = (ROOT / "backend" / "llama_client.py").read_text(encoding="utf-8")
check("chat() and chat_stream() both use _should_retry_http_status",
      _src.count("_should_retry_http_status(") >= 3)  # 1 def + 2 call sites
check("old bare transient-retry condition is gone",
      "in _TRANSIENT_HTTP_STATUSES and attempt" not in _src)
check("404 recovery path unchanged in chat()",
      "r.status_code == 404 and attempt < max_attempts - 1" in _src)
check("404 recovery path unchanged in chat_stream()",
      _src.count("r.status_code == 404 and attempt < max_attempts - 1") == 2)


# ── 8. integration: chat() vs a fake llama-server (hermetic) ───────────────
_PORT = 8999


def _resp(status, *, text="", json_data=None):
    req = httpx.Request("POST", f"http://127.0.0.1:{_PORT}/v1/chat/completions")
    if json_data is not None:
        return httpx.Response(status, json=json_data, request=req)
    return httpx.Response(status, text=text, request=req)


_OK_BODY = {"choices": [{"message": {"content": "hello"}}]}


class _FakeLlamaServer:
    """Stand-in for httpx.AsyncClient: counts POSTs, replays canned answers."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def post(self, url, json=None, timeout=None):
        self.calls += 1
        if self.calls > len(self._responses):
            raise AssertionError("fake server ran out of canned responses")
        return self._responses[self.calls - 1]


def _chat_with(responses):
    """Run LlamaClient.chat() against the fake server; return (result, error, calls)."""
    fake = _FakeLlamaServer(responses)
    client = LlamaClient()
    real_pool = client._client          # the constructor's real (unused) pool
    client._client = fake               # hermetic: no real socket is ever opened
    result, error = None, None
    try:
        with mock.patch.object(M.manager, "ensure_loaded",
                               new=mock.AsyncMock(return_value=_PORT)):
            result = asyncio.run(client.chat(
                "test:35b", [{"role": "user", "content": "hi"}]))
    except LlamaError as exc:           # expected: the no-retry policy fired
        error = exc
    except AssertionError as exc:       # fake ran dry -> retry cap is broken
        error = exc
    finally:
        # aclose the unused pool directly - never client.close(): that would
        # run manager.shutdown().
        asyncio.run(real_pool.aclose())
    return result, error, fake.calls


def _run_integration():
    # (a) two 500s then 200: succeeds on the 3rd POST, exactly 3 POSTs
    res, err, calls = _chat_with([
        _resp(500, text="internal error"),
        _resp(500, text="internal error"),
        _resp(200, json_data=_OK_BODY),
    ])
    check("chat() succeeds after two 500s", res == "hello" and err is None,
          f" res={res!r} err={err!r}")
    check("exactly 3 POSTs on the success path", calls == 3, f" calls={calls}")

    # (b) deterministic 500 forever: gives up after 3 POSTs (was 45)
    res, err, calls = _chat_with([
        _resp(500, text="internal error"),
        _resp(500, text="internal error"),
        _resp(500, text="internal error"),
        _resp(500, text="internal error"),
    ])
    check("deterministic 500 raises LlamaError", err is not None
          and isinstance(err, LlamaError), f" err={err!r}")
    check("deterministic 500 stops at 3 POSTs (not 45)", calls == 3,
          f" calls={calls}")

    # (c) overflow-shaped 500: single POST, no retry at all
    res, err, calls = _chat_with([
        _resp(500, text="the prompt exceeds the context window"),
    ])
    check("overflow 500 raises LlamaError immediately",
          isinstance(err, LlamaError), f" err={err!r}")
    check("overflow 500 makes exactly 1 POST", calls == 1, f" calls={calls}")


_run_integration()


sys.exit(h.finish("llama_client_retry"))
