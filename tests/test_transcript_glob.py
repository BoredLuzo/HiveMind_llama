# -*- coding: utf-8 -*-
"""Transkript-Experiment + Glob-Fix (2026-10-05, Sonnet blocker #1).

Frage: Wer schreibt das Chat-JSON? Beobachtung am Live-System
(TG-PROBE-Chat 16420a06):
  - POST /chats anlegt das Haupt-JSON mit 0 messages
  - ein /stream-Run mit dieser chat_id schreibt bei Completion NUR das
    SIDECAR (<...>.context.json, session-Feld), NICHT das Haupt-JSON
  - load_chat_transcript/_read_chat_json glob-t `*_<id>.json`, das auch
    das Sidecar matcht, und nimmt `matches[-1]` = alphabetisch das
    SIDECAR -> der [HISTORY-SEED] loggte source=json, las aber die
    schwache Sidecar-History.

Diese Tests pinnen den Fix: der Glob liest nur das Haupt-JSON; das
Sidecar ist ausschliesslich der Fallback via _load_chat_context.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from context.chat import _read_chat_json, history_seed  # noqa: E402


def _setup(monkeypatch, tmp: str):
    import context.chat as cc
    monkeypatch.setattr(cc, "_SESSIONS_DIR", Path(tmp))


def test_glob_prefers_main_json_over_sidecar(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _setup(monkeypatch, tmp)
        Path(tmp, "2026-10-05T00-10-04_TG-PROBE_16420a06.json").write_text(
            json.dumps({"id": "16420a06", "title": "TG-PROBE",
                        "messages": [{"role": "user", "content": "MAIN-JSON-TURN"},
                                     {"role": "assistant", "content": "RECEIVED"}]}),
            encoding="utf-8")
        Path(tmp, "2026-10-05T00-10-04_TG-PROBE_16420a06.context.json").write_text(
            json.dumps({"id": "16420a06", "title": "TG-PROBE", "session": [
                {"role": "user", "content": "SIDECAR-TURN"}]}),
            encoding="utf-8")
        data = _read_chat_json("16420a06")
        assert data["messages"][0]["content"] == "MAIN-JSON-TURN"
        # transcript comes from the main json, not the sidecar
        # (history_seed drops a TRAILING user message by design - the new
        # prompt arrives via the run - hence the assistant turn in the file)
        seed = history_seed("16420a06")
        assert seed and seed[-1]["content"] == "RECEIVED"


def test_glob_still_finds_plain_json_without_sidecar(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        _setup(monkeypatch, tmp)
        Path(tmp, "2026-10-05T00-00-00_solo_abcdef12.json").write_text(
            json.dumps({"id": "abcdef12", "title": "solo",
                        "messages": [{"role": "user", "content": "solo-turn"}]}),
            encoding="utf-8")
        assert _read_chat_json("abcdef12")["messages"][0]["content"] == "solo-turn"


def test_main_json_empty_falls_back_to_sidecar(monkeypatch):
    # exactly the TG-PROBE situation: main json 0 messages, sidecar has the
    # run-written session - history_seed must fall back WITH the honest
    # sidecar label (chat_run logs source=json|sidecar from its own probe,
    # and after the fix its primary probe returns EMPTY here, so the
    # fallback path fires)
    with tempfile.TemporaryDirectory() as tmp:
        _setup(monkeypatch, tmp)
        Path(tmp, "2026-10-05T00-10-04_TG-PROBE_16420a06.json").write_text(
            json.dumps({"id": "16420a06", "title": "TG-PROBE", "messages": []}),
            encoding="utf-8")
        Path(tmp, "2026-10-05T00-10-04_TG-PROBE_16420a06.context.json").write_text(
            json.dumps({"id": "16420a06", "title": "TG-PROBE", "session": [
                {"role": "user", "content": "SIDECAR-TURN"},
                {"role": "assistant", "content": "RECEIVED"}]}),
            encoding="utf-8")
        assert _read_chat_json("16420a06")["messages"] == []
        seed = history_seed("16420a06")
        assert len(seed) == 2 and seed[0]["content"] == "SIDECAR-TURN"


def test_glob_requires_id_suffix(monkeypatch):
    # an unrelated chat whose title merely CONTAINS the id must not match
    with tempfile.TemporaryDirectory() as tmp:
        _setup(monkeypatch, tmp)
        Path(tmp, "2026-10-05T00-00-00_other_99aabbcc.json").write_text(
            json.dumps({"id": "99aabbcc", "title": "x99aabbcc-decoy",
                        "messages": [{"role": "user", "content": "decoy"}]}),
            encoding="utf-8")
        assert _read_chat_json("aabbcc") is None
