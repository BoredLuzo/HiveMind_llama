# -*- coding: utf-8 -*-
"""Mutable round-level state carried across AgenticToolLoop POST+retry + tool-exec."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class DuoRoundState:
    """Shared mutable state mutated by both AgenticToolLoop (POST+retry) and
    duo_runner's tool-execution loop. Created once per agentic run, lives
    across all tool rounds — no manual sync needed."""

    # ── POST+retry mutable state (AgenticToolLoop modifies) ──
    think_runtime: bool = True
    inject_no_think: bool = False
    http_404_retries: int = 0
    parse_errors: int = 0
    no_extras_fallback: bool = False
    first_err_body: str = ""
    last_err_body: str = ""

    # ── Port / model bookkeeping ──
    cached_port: int | None = None
    current_port: int = 0
    exec_model: str = ""
    dtool_opts: dict = field(default_factory=dict)
    tool_read_timeout_s: float = 300.0

    # ── Reactive tool-thinking state ──
    tool_fail_streak: int = 0
    reactive_think_activated: bool = False

    # ── S2 (2026-08-23): Constrained Decoding ──
    force_grammar: bool = False


# Backward-compatible alias for code still importing AgenticDuoState
AgenticDuoState = DuoRoundState
