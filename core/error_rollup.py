# -*- coding: utf-8 -*-
"""Deterministic error rollup for the coder fix-loop (2026-09-06).

Goal: when a fix-loop repeats the SAME test/compile failures round after round,
the coder must not receive the full error text again - it needs to know "still
failing since attempt #N" instead. Pure logic, no LLM, no hallucination risk.

Identity rule (strict, no fuzzy matching):
  signature = (error_type, exact message body)
  file/line/col are separate metadata fields and NOT part of the signature.
  Any change to the message text -> new error. Better to under-deduplicate than
  to hide a genuinely new failure.

State separation:
  first_seen / attempt counters live in Python-side runtime state (per run),
  completely independent of the LLM message history. Compression of the message
  history must not reset them (see RollupState.on_message_history_compressed).

Pipeline: deterministic extraction -> rollup/dedup -> (8k tool cap downstream)
         -> LLM summarization only as the last resort.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Optional


# ── Parsing ──────────────────────────────────────────────────────────────────

# "File \"x.py\", line 42, in foo"
_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([\w.]+))?')

# Python final exception:  "TypeError: cannot add int and str"
_ERR_CLASS_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning))(?::\s*)(.*)$"
)

# pytest short summary:  "FAILED tests/test_a.py::test_1 - AssertionError: msg"
_PYTEST_FAILED_RE = re.compile(
    r"^\s*FAILED\s+(\S+?)\s+-\s+([A-Za-z_][A-Za-z0-9_.]*):\s*(.*)$"
)

# TS/ESLint:  "src/a.ts(12,4): error TS2304: msg"
_TS_RE = re.compile(
    r"^\s*(.+\.(?:ts|tsx|js|jsx|mjs|cjs))\((\d+),(\d+)\):\s*error\s+"
    r"([A-Za-z0-9_-]+):\s*(.*)$"
)

# GCC/generic path errors:  "src/a.c:12:34: error: msg"
_GCC_RE = re.compile(r"^\s*(.+?):(\d+):(\d+):\s*(?:error|fatal error):\s*(.*)$")
_GCC_LINE_RE = re.compile(r"^\s*(.+?):(\d+):\s*(?:error|fatal error):\s*(.*)$")


@dataclass
class ErrorItem:
    """One parsed failure. location is metadata, NOT part of the signature."""
    error_type: str
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    col: Optional[int] = None

    @property
    def signature(self) -> tuple:
        return (self.error_type, self.message)

    def location_str(self) -> str:
        parts = []
        if self.file:
            parts.append(self.file)
            if self.line:
                parts.append(f"{self.line}:{self.col if self.col is not None else ''}".rstrip(":"))
        return ":".join(p for p in parts if p) if parts else "(no location)"


def parse_errors(text: str) -> list:
    """Deterministic, conservative parse. Returns [] when nothing matches."""
    if not text:
        return []
    lines = text.splitlines()
    items: list = []
    pending_frame: tuple | None = None

    for line in lines:
        fm = _FRAME_RE.search(line)
        if fm:
            pending_frame = (fm.group(1), int(fm.group(2)))
            continue
        m = _PYTEST_FAILED_RE.match(line)
        if m:
            items.append(ErrorItem(
                error_type=m.group(2), message=m.group(3).strip(),
                file=m.group(1), line=None, col=None,
            ))
            continue
        m = _TS_RE.match(line)
        if m:
            items.append(ErrorItem(
                error_type=m.group(4), message=m.group(5).strip(),
                file=m.group(1), line=int(m.group(2)), col=int(m.group(3)),
            ))
            continue
        m = _GCC_RE.match(line)
        if m:
            items.append(ErrorItem(
                error_type="error", message=m.group(4).strip(),
                file=m.group(1), line=int(m.group(2)), col=int(m.group(3)),
            ))
            continue
        m = _GCC_LINE_RE.match(line)
        if m:
            items.append(ErrorItem(
                error_type="error", message=m.group(3).strip(),
                file=m.group(1), line=int(m.group(2)), col=None,
            ))
            continue
        m = _ERR_CLASS_RE.match(line)
        if m:
            file_, ln_ = (pending_frame if pending_frame else (None, None))
            items.append(ErrorItem(
                error_type=m.group(1), message=m.group(2).strip(),
                file=file_, line=ln_, col=None,
            ))
            # A frame only describes the exception directly below it.
            pending_frame = None

    # Dedupe identical signatures within one output; keep the first occurrence.
    seen: dict = {}
    for it in items:
        seen.setdefault(it.signature, it)
    return list(seen.values())


# ── Run-state (Python side, independent of message history) ─────────────────

@dataclass
class _Active:
    item: ErrorItem
    first_seen: int
    attempts: int
    reopened_of: Optional[int] = None


class RollupState:
    """first_seen/attempts live here - never in the LLM message history."""

    def __init__(self) -> None:
        self.round: int = 0
        self.active: dict = {}      # signature -> _Active
        self.resolved: dict = {}    # signature -> fixed_at_round (historical)
        # TODO: cap `resolved` size for very long runs (e.g. keep last N fixed).

    def on_message_history_compressed(self) -> None:
        """Message-history compression must NOT touch runtime state (no-op).

        Kept as an explicit marker so callers/tests document the separation:
        LLM message history may be condensed; agent runtime state may not.
        """
        return None

    def _next_round(self) -> int:
        self.round += 1
        return self.round

    def mark_all_fixed(self) -> list:
        """A clean test run: everything still active is now resolved."""
        if not self.active:
            return []
        r = self._next_round()
        out = []
        for sig in list(self.active):
            a = self.active.pop(sig)
            self.resolved[sig] = r
            out.append((sig, a.item, r))
        return out

    def ingest(self, items: list) -> dict:
        """Feed the parsed errors of one failing round.

        Returns {round, changed, entries, fixed} where entries describe every
        present signature and `fixed` lists signatures that disappeared.
        `changed` is True only when at least one signature is a repeat
        (persistent/reopened) or something got fixed - i.e. a rollup round.
        """
        if not items:
            return {"round": self.round, "changed": False, "entries": [], "fixed": []}
        r = self._next_round()
        present = {}
        for it in items:
            present.setdefault(it.signature, it)

        entries: list = []
        for sig, item in present.items():
            a = self.active.get(sig)
            if a is not None:
                a.attempts += 1
                a.item = item
                status = "persistent"
                if a.reopened_of is not None and a.reopened_of != a.first_seen:
                    pass  # stays reopened while present; keep flag below
                entries.append({
                    "status": status, "item": item, "sig": sig,
                    "first_seen": a.first_seen, "attempts": a.attempts,
                    "reopened_of": a.reopened_of,
                })
            elif sig in self.resolved:
                # Deliberate: a reopened failure starts a NEW attempt cycle
                # (attempts=1). The coder should read "attempt #1 after a
                # regression that was fixed in #N" - not a continued old
                # counter ("#7 at the same spot"). reopened_of keeps the
                # history link to the previous fix.
                a = _Active(item=item, first_seen=r, attempts=1,
                            reopened_of=self.resolved[sig])
                self.active[sig] = a
                entries.append({
                    "status": "reopened", "item": item, "sig": sig,
                    "first_seen": r, "attempts": 1,
                    "reopened_of": a.reopened_of,
                })
            else:
                self.active[sig] = _Active(item=item, first_seen=r, attempts=1)
                entries.append({
                    "status": "new", "item": item, "sig": sig,
                    "first_seen": r, "attempts": 1, "reopened_of": None,
                })

        fixed: list = []
        for sig in list(self.active):
            if sig not in present:
                a = self.active.pop(sig)
                self.resolved[sig] = r
                fixed.append((sig, a.item, r))

        changed = any(e["status"] in ("persistent", "reopened") for e in entries) or bool(fixed)
        return {"round": r, "changed": changed, "entries": entries, "fixed": fixed}


def _loc(item) -> str:
    return item.location_str()


def render_rollup(res: dict, sort_key=lambda e: (e["item"].error_type, e["item"].message)) -> list:
    """Deterministic compact lines for a rollup result (for a FAILED round)."""
    lines: list = []
    for e in sorted(res["entries"], key=sort_key):
        it = e["item"]
        if e["status"] == "new":
            lines.append(f"[NEW] {it.error_type}: {it.message} @ {_loc(it)}")
        elif e["status"] == "persistent":
            lines.append(
                f"[PERSISTS] {it.error_type}: {it.message} @ {_loc(it)} "
                f"(first seen attempt #{e['first_seen']}, fix attempt #{e['attempts']})"
            )
        elif e["status"] == "reopened":
            lines.append(
                f"[REOPENED] {it.error_type}: {it.message} @ {_loc(it)} "
                f"(was fixed in attempt #{e['reopened_of']})"
            )
    for sig, it, r in res.get("fixed", []):
        lines.append(f"[FIXED] {it.error_type}: {it.message} @ {_loc(it)} (attempt #{r})")
    return lines


# ── Per-run registry ─────────────────────────────────────────────────────────

_states: dict = {}
_states_lock = threading.RLock()
_STATE_CAP = 32  # TODO: prune strategy for very long-lived server processes.


def get_run_state(run_id: str) -> RollupState:
    with _states_lock:
        st = _states.get(run_id)
        if st is None:
            st = RollupState()
            _states[run_id] = st
            if len(_states) > _STATE_CAP:
                # drop oldest keys (insertion order preserved on CPython 3.7+)
                for _old in list(_states)[: len(_states) - _STATE_CAP]:
                    _states.pop(_old, None)
        return st


def clear_run_state(run_id: str) -> None:
    with _states_lock:
        _states.pop(run_id, None)
