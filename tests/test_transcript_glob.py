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


def test_provenance_origin_survives_empty_normalization(monkeypatch):
    # SONNET EDGE CASE (2026-10-05): main json holds ONLY the edited
    # trailing user message -> normalization empties the seed. The ORIGIN
    # must stay 'json' (not fall through to 'none'), so chat_run clears the
    # stale memory instead of resurrecting the old history.
    with tempfile.TemporaryDirectory() as tmp:
        _setup(monkeypatch, tmp)
        _write(tmp, REAL_MAIN, {"id": "16420a06", "title": "TG-PROBE", "messages": [
            {"role": "user", "content": "NEW-EDITED-TURN"}]})
        seed, source = history_seed_provenance("16420a06")
        assert seed == [] and source == "json"


def test_edge_case_no_sidecar_resurrection(monkeypatch):
    # THE FULL DEMANDED CASE: main json [u] only, sidecar [u,a,u,a].
    # Expected: source=json, N=0, NOTHING from the sidecar.
    with tempfile.TemporaryDirectory() as tmp:
        cc._SESSIONS_DIR = Path(tmp)
        _write(tmp, REAL_MAIN, {"id": "16420a06", "title": "TG-PROBE", "messages": [
            {"role": "user", "content": "NEW-EDITED-TURN"}]})
        _write(tmp, REAL_SIDECAR, {"id": "16420a06", "title": "TG-PROBE", "session": [
            {"role": "user", "content": "OLD-U"},
            {"role": "assistant", "content": "OLD-A"},
            {"role": "user", "content": "OLD-U2"},
            {"role": "assistant", "content": "OLD-A2"}]})
        seed, source = history_seed_provenance("16420a06")
        assert source == "json"
        assert seed == []
        blob = json.dumps(seed, default=str)
        assert "OLD-" not in blob


# ── chat_run lazy-import tripwire (2026-10-05, ImportError live): chat_run
#    pulls names from the server namespace - a name added to that list but
#    not defined in server.py only explodes at runtime. AST-pin: every name
#    chat_run requests from server must exist as a top-level binding there. ──

def test_chat_run_server_import_names_all_resolve():
    import ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cr = ast.parse(open(os.path.join(root, "core", "chat_run.py"), encoding="utf-8").read())
    sv = ast.parse(open(os.path.join(root, "server.py"), encoding="utf-8").read())
    # names chat_run pulls from server
    requested = set()
    for node in ast.walk(cr):
        if isinstance(node, ast.ImportFrom) and node.module == "server":
            for a in node.names:
                requested.add(a.asname or a.name)
    assert requested, "chat_run's server import list vanished - test is blind"
    # top-level bindings in server.py: defs, assigns, imports, and
    # from-imports (incl. aliases). Try-blocks at module level count too -
    # server binds _WEBSEARCH_AVAILABLE/_VRAM_LOOKUP_GB inside try/except
    # (optional deps).
    bound = set()

    def _collect(stmts):
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        bound.add(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                bound.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    bound.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, ast.Try):
                _collect(node.body)
                _collect(node.orelse)
                _collect(node.finalbody)
                for h in node.handlers:
                    _collect(h.body)
            elif isinstance(node, ast.If):
                _collect(node.body)
                _collect(node.orelse)

    _collect(sv.body)
    missing = sorted(n for n in requested if n not in bound)
    assert not missing, f"chat_run imports names server.py never binds: {missing}"


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
