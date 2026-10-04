# -*- coding: utf-8 -*-
"""Chat search endpoint (SEARCHBAR PHASE 1, 2026-10-04).

Runs the REAL router handler against a temp sessions dir: title hits,
message-content hits with snippet, the short-query guard, the limit cap,
and that .context.json sidecars are skipped.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routers.chats as rc  # noqa: E402


def _setup(tmp: str):
    import threading
    rc._state._SESSIONS_DIR = Path(tmp)
    rc._cache_loaded = False
    if rc._cache_lock is None:  # None until server startup initializes it
        rc._cache_lock = threading.RLock()
    rc._chats_cache.clear()


def _write(tmp: str, name: str, chat: dict):
    Path(tmp, name).write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")


def test_title_match(tmp=None):
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        _write(tmp, "2026-10-04T12-00-00_alpha-chat_aa11bb22.json",
               {"id": "aa11bb22", "title": "Alpha Chat",
                "updated_at": "2026-10-04T12:00:00", "messages": []})
        out = asyncio.run(rc.search_chats(q="alpha"))
        assert out["ok"] and len(out["results"]) == 1
        r = out["results"][0]
        assert r["match_in"] == "title" and r["id"] == "aa11bb22"


def test_message_content_match_with_snippet(tmp=None):
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        _write(tmp, "2026-10-04T12-00-00_beta_b33cc44d.json",
               {"id": "b33cc44d", "title": "Beta",
                "updated_at": "2026-10-04T12:00:00",
                "messages": [
                    {"role": "user", "content": "please find the ZEBRA-42 token for me"},
                    {"role": "assistant", "content": "sure"},
                ]})
        out = asyncio.run(rc.search_chats(q="zebra-42"))
        assert len(out["results"]) == 1
        r = out["results"][0]
        assert r["match_in"] == "messages"
        assert "ZEBRA-42" in r["snippet"]


def test_short_query_returns_empty(tmp=None):
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        out = asyncio.run(rc.search_chats(q="x"))
        assert out["results"] == []


def test_case_insensitive_and_limit(tmp=None):
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        for i in range(5):
            _write(tmp, f"2026-10-04T12-0{i}-00_gamma_d{i}e5f6a7b.json",
                   {"id": f"d{i}e5f6a7b", "title": f"Gamma {i}",
                    "updated_at": f"2026-10-04T12:0{i}:00", "messages": []})
        out = asyncio.run(rc.search_chats(q="GAMMA", limit=3))
        assert len(out["results"]) == 3


def test_context_sidecars_skipped(tmp=None):
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        _write(tmp, "2026-10-04T12-00-00_delta_99aabbcc.context.json",
               {"id": "99aabbcc", "title": "Delta Sidecar",
                "messages": [{"role": "user", "content": "needle"}]})
        out = asyncio.run(rc.search_chats(q="needle"))
        assert out["results"] == []


def test_route_registered_before_catch_all():
    # ORDER PIN: /search must stay BEFORE /{chat_id} in the route table,
    # or the catch-all swallows it and search 404s into a chat lookup.
    paths = [getattr(r, "path", "") for r in rc.router.routes]
    _search = next(i for i, p in enumerate(paths) if p.endswith("/search"))
    _catch = next(i for i, p in enumerate(paths) if p.endswith("/{chat_id}"))
    assert _search < _catch
