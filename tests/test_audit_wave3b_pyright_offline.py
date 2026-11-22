# -*- coding: utf-8 -*-
"""Regression suite for Audit Wave 3B: Pyright type-gate fixes & offline websearch fast-fail.

Covers:
  1. core/duo_compress_cycle.py: _evict_condensed_from_read_guard accepts sets and lists cleanly
  2. core/tool_loop.py: _last_compressed_messages narrows properly across retry -> next round
  3. backend/manager_load.py: load()/ensure_loaded() explicit post-waiter RuntimeError guards & None-safe retry helpers
  4. tools/websearch.py: ConnectError / ConnectTimeout fast-fail without duplicate connect attempts or offline wiki hangs
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


# ── 1. core/duo_compress_cycle.py set/list Iterable support ──────────────────
def test_evict_condensed_accepts_set_and_list() -> None:
    from core.duo_compress_cycle import _evict_condensed_from_read_guard
    from tools.runner import _files_read_in_run
    from utils.file import normalize_tool_path

    ws = str(ROOT)
    p1 = normalize_tool_path("server.py", ws)
    p2 = normalize_tool_path("run.py", ws)
    tok = _files_read_in_run.set({p1, p2})
    try:
        _evict_condensed_from_read_guard({"server.py"}, ws)
        after_set = set(_files_read_in_run.get(set()) or set())
        _evict_condensed_from_read_guard(["run.py"], ws)
        after_list = set(_files_read_in_run.get(set()) or set())
    finally:
        _files_read_in_run.reset(tok)

    check(
        "_evict_condensed_from_read_guard accepts both set and list iterables",
        p1 not in after_set and p2 in after_set and after_list == set(),
        f"(after_set={after_set}, after_list={after_list})",
    )


# ── 2. core/tool_loop.py compressed messages handoff ─────────────────────────
def test_tool_loop_compressed_messages_handoff() -> None:
    from core.tool_loop import ToolLoop, ToolLoopConfig

    seen_msgs: list[list[dict]] = []

    class _FakeLoop(ToolLoop):
        async def _post_with_retry(self, payload: dict):
            seen_msgs.append(list(payload.get("messages") or []))
            if len(seen_msgs) == 1:
                self._last_compressed_messages = [{"role": "user", "content": "shrunk"}]
                return None, None, [], []
            return {"role": "assistant", "content": "done"}, "done", [], []

    cfg = ToolLoopConfig(model="test-model", stream=False, require_tool_call=False, max_rounds=4)

    async def _run():
        events = []
        async with httpx.AsyncClient(verify=False, trust_env=False) as client:
            loop = _FakeLoop(config=cfg, http_client=client, port=1)
            async for ev in loop.run([{"role": "user", "content": "original huge prompt"}]):
                events.append(ev)
            return events, loop._last_compressed_messages

    events, leftover_compressed = asyncio.run(_run())
    check(
        "ToolLoop carries _last_compressed_messages into subsequent round cleanly",
        len(seen_msgs) == 2
        and seen_msgs[0][0]["content"] == "original huge prompt"
        and seen_msgs[1] == [{"role": "user", "content": "shrunk"}]
        and leftover_compressed is None
        and any(e.get("type") == "token" and e.get("content") == "done" for e in events),
        f"(seen_msgs={seen_msgs}, events={events})",
    )


# ── 3. backend/manager_load.py waiter guards ─────────────────────────────────
def test_manager_load_waiter_guards() -> None:
    src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    methods: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in ("load", "ensure_loaded"):
            methods[node.name] = node

    def _ends_with_runtime_error(fn: ast.AsyncFunctionDef | None) -> bool:
        if fn is None or not fn.body:
            return False
        last = fn.body[-1]
        return (
            isinstance(last, ast.Raise)
            and isinstance(last.exc, ast.Call)
            and isinstance(last.exc.func, ast.Name)
            and last.exc.func.id == "RuntimeError"
        )

    check(
        "manager_load.load and ensure_loaded end with explicit RuntimeError guards",
        _ends_with_runtime_error(methods.get("load"))
        and _ends_with_runtime_error(methods.get("ensure_loaded")),
    )


# ── 4. tools/websearch.py offline fast-fail ──────────────────────────────────
def test_websearch_offline_fast_fail() -> None:
    import tools.websearch as ws

    # 4a) check_status short-circuits on ConnectError and ConnectTimeout in 1 call
    for exc_cls in (httpx.ConnectError, httpx.ConnectTimeout):
        calls = 0

        class _DeadClient:
            async def get(self, *args, **kwargs):
                nonlocal calls
                calls += 1
                raise exc_cls("host unreachable")

        orig_en = ws._SEARXNG_ENABLED
        orig_host = ws._SEARXNG_HOST
        try:
            ws._SEARXNG_ENABLED = True
            ws._SEARXNG_HOST = "http://127.0.0.1:59999"

            async def _fake_client():
                return _DeadClient()

            with mock.patch.object(ws, "_get_client_async", _fake_client):
                res = asyncio.run(ws.check_status())
        finally:
            ws._SEARXNG_ENABLED = orig_en
            ws._SEARXNG_HOST = orig_host

        check(
            f"check_status short-circuits on {exc_cls.__name__} with 1 HTTP attempt",
            res.get("ok") is False and res.get("reason") == "unreachable" and calls == 1,
            f"(calls={calls}, res={res})",
        )

    # 4b) _fetch_searxng_json: ConnectTimeout does NOT retry (1 call), ReadTimeout retries once (2 calls)
    ct_calls = 0

    class _CtClient:
        async def get(self, *args, **kwargs):
            nonlocal ct_calls
            ct_calls += 1
            raise httpx.ConnectTimeout("connect timeout")

    async def _fake_ct_client():
        return _CtClient()

    with mock.patch.object(ws, "_get_client_async", _fake_ct_client):
        data_ct, err_ct = asyncio.run(ws._fetch_searxng_json("test", 3))
    check(
        "_fetch_searxng_json does not retry ConnectTimeout (1 attempt)",
        data_ct is None and "connect timeout" in (err_ct or "").lower() and ct_calls == 1,
        f"(ct_calls={ct_calls}, err={err_ct})",
    )

    rt_calls = 0

    class _RtClient:
        async def get(self, *args, **kwargs):
            nonlocal rt_calls
            rt_calls += 1
            raise httpx.ReadTimeout("read timeout")

    async def _fake_rt_client():
        return _RtClient()

    with mock.patch.object(ws, "_get_client_async", _fake_rt_client):
        data_rt, err_rt = asyncio.run(ws._fetch_searxng_json("test", 3))
    check(
        "_fetch_searxng_json still retries ReadTimeout once (2 attempts)",
        data_rt is None and "2 attempts" in (err_rt or "") and rt_calls == 2,
        f"(rt_calls={rt_calls}, err={err_rt})",
    )

    # 4c) web_search skips _wiki_direct when rescue search failed due to network error
    wiki_calls = 0

    async def _fake_wiki(*_a, **_k):
        nonlocal wiki_calls
        wiki_calls += 1
        return "wiki"

    fetch_seq = [
        ({"results": [], "unresponsive_engines": ["bing"]}, None),
        (None, "[web_search: connect timeout]"),
    ]

    async def _fake_fetch(*_a, **_k):
        return fetch_seq.pop(0)

    orig_en2 = ws._SEARXNG_ENABLED
    orig_eng2 = ws._SEARXNG_ENGINES
    orig_cache2 = dict(ws._SEARCH_CACHE)
    try:
        ws._SEARXNG_ENABLED = True
        ws._SEARXNG_ENGINES = ""
        ws._SEARCH_CACHE.clear()
        with mock.patch.object(ws, "_fetch_searxng_json", _fake_fetch), \
             mock.patch.object(ws, "_wiki_direct", _fake_wiki):
            out = asyncio.run(ws.web_search("offline rescue test query", 3))
    finally:
        ws._SEARXNG_ENABLED = orig_en2
        ws._SEARXNG_ENGINES = orig_eng2
        ws._SEARCH_CACHE.clear()
        ws._SEARCH_CACHE.update(orig_cache2)

    check(
        "web_search skips offline _wiki_direct fallback when rescue request errored",
        wiki_calls == 0 and "No results" in out,
        f"(wiki_calls={wiki_calls}, out={out[:80]!r})",
    )


if __name__ == "__main__":
    test_evict_condensed_accepts_set_and_list()
    test_tool_loop_compressed_messages_handoff()
    test_manager_load_waiter_guards()
    test_websearch_offline_fast_fail()
    sys.exit(h.finish("audit_wave3b_pyright_offline"))
