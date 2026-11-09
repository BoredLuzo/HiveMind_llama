# -*- coding: utf-8 -*-
"""AgenticToolLoop generic-5xx retry policy = F3 (task F11, 2026-11-08).

Live bug (duo_runner builds the coder loop with max_post_attempts=20): the
generic 5xx fall-through in AgenticToolLoop.post_with_retry re-POSTed the
FULL prompt body up to 20 times on a deterministic 500/502/504 - hours of
repeated prefill while the run looked alive. The F3 policy from
backend/llama_client._should_retry_http_status now applies:

  - an overflow-shaped error body never retries (fail immediately)
  - plain 500/502/504 are capped at 3 total POST attempts
  - 408/429/503 keep the long retry (up to max_post_attempts, unchanged)
  - the self-bounded 500 recovery branches (parse-json, CallExpression)
    and the 404/ConnectError/ReadTimeout paths are untouched

Hermetic: httpx.MockTransport fake server, no llama-server, no model.
asyncio.sleep is patched to a no-op (the real backoffs sum to >10 s).

Run: python tests/test_agentic_5xx_policy.py
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

from core.agentic_tool_loop import AgenticToolLoop, _f3_should_retry_status  # noqa: E402
from core.tool_loop import ToolLoopConfig  # noqa: E402
from core.agentic_duo_state import DuoRoundState  # noqa: E402

_PORT = 8765


# ── 1. wrapper passthrough: real helper importable, same decisions ─────────
check("F3 wrapper: plain 500 attempt 2 -> False",
      _f3_should_retry_status(500, "internal error", 2, 20) is False)
check("F3 wrapper: plain 500 attempt 1 -> True",
      _f3_should_retry_status(500, "internal error", 1, 20) is True)
check("F3 wrapper: overflow 500 attempt 0 -> False",
      _f3_should_retry_status(500, "the prompt exceeds the context window", 0, 20) is False)
check("F3 wrapper: 503 keeps long retry",
      _f3_should_retry_status(503, "", 10, 20) is True)


# ── 2. local fallback predicate (ImportError path) ─────────────────────────
def _fallback_result(status, body, attempt, max_attempts):
    saved = sys.modules.get("backend.llama_client")
    sys.modules["backend.llama_client"] = None   # forces ImportError on import
    try:
        return _f3_should_retry_status(status, body, attempt, max_attempts)
    finally:
        if saved is None:
            sys.modules.pop("backend.llama_client", None)
        else:
            sys.modules["backend.llama_client"] = saved


check("fallback: plain 500 attempt 2 -> False",
      _fallback_result(500, "internal error", 2, 20) is False)
check("fallback: plain 500 attempt 1 -> True",
      _fallback_result(500, "internal error", 1, 20) is True)
check("fallback: overflow 500 attempt 0 -> False",
      _fallback_result(500, "context window exceeded", 0, 20) is False)
check("fallback: 503 long retry at attempt 18 -> True",
      _fallback_result(503, "context", 18, 20) is True)
check("fallback: 503 at last attempt (19 of 20) -> False",
      _fallback_result(503, "", 19, 20) is False)
check("fallback: non-transient 416 -> False",
      _fallback_result(416, "", 0, 20) is False)


# ── 3. behavior: AgenticToolLoop.post_with_retry vs a fake llama-server ────
async def _no_sleep(_delay=0):
    return None


class _Ctx:
    """Minimal ctx for post_with_retry: never aborted, no chat id."""

    chat_id = None

    def aborted(self):
        return False

    def is_aborted_chat(self, _chat_id):
        return False


_MSGS = [{"role": "system", "content": "you are a coder"},
         {"role": "user", "content": "do the task"}]
_PAYLOAD = {"model": "fake:1b",
            "messages": [{"role": "user", "content": "do the task"}],
            "tools": [], "stream": True}


class _FakeServer:
    """MockTransport handler: counts POSTs, replays canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls > len(self._responses):
            raise AssertionError("fake server ran out of canned responses")
        return self._responses[self.calls - 1]


def _sse_200(content="hi"):
    body = (f'data: {{"choices":[{{"delta":{{"content":"{content}"}}}}]}}\n\n'
            "data: [DONE]\n\n")
    req = httpx.Request("POST", f"http://127.0.0.1:{_PORT}/v1/chat/completions")
    return httpx.Response(200, text=body,
                          headers={"content-type": "text/event-stream"},
                          request=req)


def _resp(status, text):
    req = httpx.Request("POST", f"http://127.0.0.1:{_PORT}/v1/chat/completions")
    return httpx.Response(status, text=text, request=req)


def _drive(responses, *, max_post_attempts=20):
    """Run post_with_retry against the fake server; return (result, error, calls)."""
    fake = _FakeServer(responses)
    round_state = DuoRoundState(current_port=_PORT, exec_model="fake:1b",
                                think_runtime=False)
    loop = AgenticToolLoop(
        config=ToolLoopConfig(stream=True, max_post_attempts=max_post_attempts,
                              model="fake:1b"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        round_state=round_state, workspace="", run_id="",
    )
    result, error = None, None
    try:
        with mock.patch("asyncio.sleep", new=_no_sleep):
            result = asyncio.run(loop.post_with_retry(
                dict(_PAYLOAD), dtool_msgs=list(_MSGS), ctx=_Ctx(), _parts=[]))
    except RuntimeError as exc:
        error = exc
    finally:
        asyncio.run(loop._client.aclose())
    return result, error, fake.calls


def _run_behavior():
    # (a) deterministic plain 500 forever: 3 total POSTs (was up to 20)
    _, err, calls = _drive([_resp(500, "internal error")] * 5)
    check("deterministic 500 stops at 3 POSTs (not 20)", calls == 3,
          f" calls={calls}")
    check("deterministic 500 raises the stream-not-received error",
          err is not None and "stream not received after 3 attempts" in str(err)
          and "internal error" in str(err), f" err={err!r}")

    # (b) overflow-shaped 500: 1 POST, fail immediately
    _, err, calls = _drive([_resp(500, "the prompt exceeds the available "
                                       "context window")] * 3)
    check("overflow 500 fails after exactly 1 POST", calls == 1, f" calls={calls}")
    check("overflow 500 error carries the body",
          err is not None and "context window" in str(err), f" err={err!r}")

    # (c) overflow-shaped 502 ('too many tokens'): 1 POST
    _, err, calls = _drive([_resp(502, "error: too many tokens")] * 3)
    check("overflow 502 fails after exactly 1 POST", calls == 1, f" calls={calls}")

    # (d) plain 502 / 504: capped at 3 total POSTs
    _, err, calls = _drive([_resp(502, "bad gateway")] * 5)
    check("plain 502 stops at 3 POSTs", calls == 3, f" calls={calls}")
    _, err, calls = _drive([_resp(504, "gateway timeout")] * 5)
    check("plain 504 stops at 3 POSTs", calls == 3, f" calls={calls}")

    # (e) 503 keeps the LONG retry: with max_post_attempts=4 it runs all 4
    #     POSTs (a 3-cap would stop after 3). Fails via the last-attempt raise.
    _, err, calls = _drive([_resp(503, "model loading")] * 6, max_post_attempts=4)
    check("503 keeps the long retry (4 POSTs at max=4)", calls == 4,
          f" calls={calls}")
    check("503 failure raises the last-attempt server error",
          err is not None and "llama-server 503" in str(err), f" err={err!r}")

    # (f) 503 with an overflow-ish body still retries (long-retry class wins,
    #     same as F3 in llama_client)
    _, err, calls = _drive([_resp(503, "context window exceeded")] * 6,
                           max_post_attempts=4)
    check("503 overflow-ish body still retries (long-retry class)", calls == 4,
          f" calls={calls}")

    # (g) two plain 500s then 200: succeeds on the 3rd POST
    with mock.patch("core.telemetry.append_round"), \
            mock.patch("core.budgets.update_cpt_ema"):
        res, err, calls = _drive([_resp(500, "internal error"),
                                  _resp(500, "internal error"),
                                  _sse_200("hi")])
    check("succeeds after two 500s", res is not None and err is None
          and res.get("dr_stream_ok") is True, f" err={err!r}")
    check("exactly 3 POSTs on the success path", calls == 3, f" calls={calls}")
    check("assistant content parsed from SSE",
          res is not None and (res.get("dr_msg") or {}).get("content") == "hi",
          f" msg={res.get('dr_msg') if res else None}")


_run_behavior()


# ── 4. source guards: policy wired, recovery/404 paths untouched ───────────
_src = (ROOT / "core" / "agentic_tool_loop.py").read_text(encoding="utf-8")
check("generic 5xx branch consults the F3 helper",
      "_f3_should_retry_status(" in _src and _src.count("_f3_should_retry_status(") >= 2)
check("F3 policy reference is documented in-file",
      "backend/llama_client._should_retry_http_status" in _src)
check("500 parse-json recovery branch untouched",
      '500-parse-json' in _src and '"parse" in _err_body.lower()' in _src)
check("CallExpression recovery branch untouched",
      '"CallExpression" in _err_body' in _src)
check("404 rebinding recovery untouched",
      "http_404_retries" in _src)
check("last-attempt 5xx still falls through to the raise",
      "and _post_attempt < self.cfg.max_post_attempts - 1:" in _src)


sys.exit(h.finish("agentic_5xx_policy"))
