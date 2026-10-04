# -*- coding: utf-8 -*-
"""Transcript provenance (2026-10-05, TG-PROBE experiment + Sonnet audit).

Live experiment (chat 16420a06): POST /chats creates the main json with 0
messages; a /stream run writes ONLY the sidecar (.context.json session) at
completion. The seed then logged `source=json seeded 2` although the main
json was empty - history_seed() falls back to the sidecar INTERNALLY and
chat_run labelled the result 'json'. THE LABEL LIED.

Corrected diagnosis (Sonnet audit): the sidecar NEVER matches the glob
`*_<id>.json` (it ends in `.context.json`), and `sorted()[-1]` picks the
MAIN json (`.` < `c`... verified with real filenames below). The glob
exclusion added first is a harmless no-op; the REAL fix is
history_seed_provenance() with honest labels, used by chat_run.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import context.chat as cc  # noqa: E402
from context.chat import (  # noqa: E402
    _read_chat_json,
    history_seed,
    history_seed_provenance,
)

# REAL filenames from the live experiment
REAL_SIDECAR = "2026-10-05T00-10-04_TG-PROBE_16420a06.context.json"
REAL_MAIN = "2026-10-05T00-10-04_TG-PROBE_16420a06.json"


def _setup(monkeypatch, tmp: str):
    monkeypatch.setattr(cc, "_SESSIONS_DIR", Path(tmp))


def _write(tmp: str, name: str, data: dict):
    Path(tmp, name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_real_filenames_sidecar_never_matches_glob():
    # Sonnet's demand: verify with the REAL file names, not synthetic ones.
    import fnmatch
    assert not fnmatch.fnmatch(REAL_SIDECAR, "*_16420a06.json"), \
        "sidecar matching would resurrect the false diagnosis"
    assert fnmatch.fnmatch(REAL_MAIN, "*_16420a06.json")
    # and sorted()[-1] over the real pair picks the MAIN json anyway -
    # the glob exclusion added first was a no-op, kept as defense only
    both = sorted([REAL_SIDECAR, REAL_MAIN])
    assert both[-1] == REAL_MAIN


def test_read_json_returns_main_with_both_files_present():
    # with the REAL name pair: main json (0 msgs, as POST /chats creates it)
    # plus sidecar with session turns
    with tempfile.TemporaryDirectory() as tmp:
        cc._SESSIONS_DIR = Path(tmp)
        _write(tmp, REAL_MAIN, {"id": "16420a06", "title": "TG-PROBE", "messages": []})
        _write(tmp, REAL_SIDECAR, {"id": "16420a06", "title": "TG-PROBE", "session": [
            {"role": "user", "content": "SIDECAR-TURN"},
            {"role": "assistant", "content": "RECEIVED"}]})
        assert _read_chat_json("16420a06")["messages"] == []


def test_provenance_honest_on_empty_main_json():
    # THE BUG CASE: main json empty, sidecar has the history - source MUST
    # be 'sidecar' (the old code logged 'json' here).
    with tempfile.TemporaryDirectory() as tmp:
        cc._SESSIONS_DIR = Path(tmp)
        _write(tmp, REAL_MAIN, {"id": "16420a06", "title": "TG-PROBE", "messages": []})
        _write(tmp, REAL_SIDECAR, {"id": "16420a06", "title": "TG-PROBE", "session": [
            {"role": "user", "content": "SIDECAR-TURN"},
            {"role": "assistant", "content": "RECEIVED"}]})
        seed, source = history_seed_provenance("16420a06")
        assert source == "sidecar"
        assert len(seed) == 2 and seed[0]["content"] == "SIDECAR-TURN"


def test_provenance_json_when_main_has_turns(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _setup(monkeypatch, tmp)
        _write(tmp, REAL_MAIN, {"id": "16420a06", "title": "TG-PROBE", "messages": [
            {"role": "user", "content": "MAIN-TURN-1"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "MAIN-TURN-2 (would be dropped as trailing)"},
        ]})
        seed, source = history_seed_provenance("16420a06")
        assert source == "json"
        # trailing user dropped (it IS the new prompt), earlier turns kept
        assert seed[-1]["content"] == "ok"


def test_provenance_none_when_neither_has_turns(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _setup(monkeypatch, tmp)
        _write(tmp, REAL_MAIN, {"id": "16420a06", "title": "TG-PROBE", "messages": []})
        seed, source = history_seed_provenance("16420a06")
        assert seed == [] and source == "none"


def test_history_seed_compat_matches_provenance(monkeypatch):
    # the compat wrapper and the provenance probe must never disagree on
    # WHICH turns are seeded (only the label differs)
    with tempfile.TemporaryDirectory() as tmp:
        cc._SESSIONS_DIR = Path(tmp)
        _write(tmp, REAL_MAIN, {"id": "16420a06", "title": "TG-PROBE", "messages": [
            {"role": "user", "content": "t1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "t2"},
            {"role": "assistant", "content": "a2"}]})
        seed, source = history_seed_provenance("16420a06")
        assert source == "json"
        assert history_seed("16420a06") == seed
