"""Budgets — one resolver per run, derived sizes only (2026-10-14).

Sonnet review principle: nothing branches on model classes, everything is
derived from measured sizes, settings only override. Budgets.resolve() takes
ONLY n_ctx, the measured chars-per-token (EMA) and max_output, plus settings,
and produces every cap the tool pipeline needs. Fractions and named
floors/ceilings live in tools/limits.py — that file holds the only hard
numbers in the codebase.

Freeze rule: everything that becomes schema-visible (read caps, tool list,
schema text) is resolved ONCE per run and then never changed — mid-run
changes would rewrite the sent prefix and kill the llama.cpp KV cache.
Live-adjustable (not in the prefix): the cpt EMA, the compression headroom
trigger, the injection budget.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, asdict

from tools.limits import (
    CPT_DEFAULT,
    TOOL_RESULT_FRAC, TOOL_RESULT_MIN, TOOL_RESULT_MAX,
    READ_BUDGET_FRAC, READ_CHAR_MIN, READ_CHAR_MAX,
    SCHEMA_FRAC, SCHEMA_MIN, SCHEMA_MAX,
    INJECTION_FRAC, INJECTION_MIN, INJECTION_MAX,
    ANCHOR_MIN, ANCHOR_MAX, OUTLINE_PER_ANCHOR, AVG_LINE_CHARS,
    COMPRESS_RESERVE_CTX_FRAC, COMPRESS_TRIGGER_RESERVE_MULT,
    COMPRESS_REST_RESERVE_MULT, COMPRESS_GROWTH_TOKENS_DEFAULT,
    EXEC_RESULT_CAP_FALLBACK,
)


def _frac(n_ctx: int, cpt: float, frac: float, lo: int, hi: int) -> int:
    return int(max(lo, min(hi, frac * n_ctx * cpt)))


@dataclass(frozen=True)
class Budgets:
    n_ctx: int
    cpt: float
    max_out: int
    # char caps (schema-visible — frozen for the run)
    tool_result_cap: int
    read_char_cap: int
    schema_budget: int
    # injection pool (schema-visible cap per message; the pool itself may
    # tighten live, the per-message cap must not)
    injection_budget: int
    injection_msg_cap: int
    anchor_lines: int
    outline_items: int
    # compression headroom (live, not in the prefix)
    compress_trigger_tokens: int   # guard_tokens above this -> compress
    compress_rest_tokens: int      # guard_tokens below this -> at rest
    growth_tokens: int
    # provenance
    overrides_used: tuple

    def as_dict(self) -> dict:
        return asdict(self)


def resolve(n_ctx: int, cpt: float | None = None, max_out: int = 4096,
            settings: dict | None = None) -> Budgets:
    """Resolve every budget from measured sizes. settings values named
    "budget_<field>" override the derivation (None/absent = auto)."""
    settings = settings or {}
    n_ctx = max(1024, int(n_ctx))
    cpt = float(cpt) if cpt and cpt > 1 else CPT_DEFAULT
    max_out = max(256, int(max_out))
    used: list = []

    def _ov(name: str, auto: int) -> int:
        v = settings.get(f"budget_{name}")
        if isinstance(v, (int, float)) and v:
            used.append(name)
            return int(v)
        return auto

    tool_result_cap = _ov("tool_result_cap", _frac(n_ctx, cpt, TOOL_RESULT_FRAC,
                                                   TOOL_RESULT_MIN, TOOL_RESULT_MAX))
    read_char_cap = _ov("read_char_cap", _frac(n_ctx, cpt, READ_BUDGET_FRAC,
                                               READ_CHAR_MIN, READ_CHAR_MAX))
    schema_budget = _ov("schema_budget", _frac(n_ctx, cpt, SCHEMA_FRAC,
                                               SCHEMA_MIN, SCHEMA_MAX))
    injection_budget = _ov("injection_budget", _frac(n_ctx, cpt, INJECTION_FRAC,
                                                     INJECTION_MIN, INJECTION_MAX))
    injection_msg_cap = max(300, min(4000, injection_budget // 3))
    anchor_lines = _ov("anchor_lines", max(ANCHOR_MIN, min(
        ANCHOR_MAX, injection_budget // (AVG_LINE_CHARS * 8))))
    outline_items = _ov("outline_items", anchor_lines * OUTLINE_PER_ANCHOR)

    growth = int(settings.get("budget_compress_growth_tokens")
                 or COMPRESS_GROWTH_TOKENS_DEFAULT)
    reserve = min(max_out, int(COMPRESS_RESERVE_CTX_FRAC * n_ctx)) + growth
    compress_trigger = max(512, int(n_ctx - COMPRESS_TRIGGER_RESERVE_MULT * reserve))
    compress_rest = max(512, int(n_ctx - COMPRESS_REST_RESERVE_MULT * reserve))

    return Budgets(
        n_ctx=n_ctx, cpt=round(cpt, 3), max_out=max_out,
        tool_result_cap=tool_result_cap,
        read_char_cap=read_char_cap,
        schema_budget=schema_budget,
        injection_budget=injection_budget,
        injection_msg_cap=injection_msg_cap,
        anchor_lines=anchor_lines,
        outline_items=outline_items,
        compress_trigger_tokens=compress_trigger,
        compress_rest_tokens=compress_rest,
        growth_tokens=growth,
        overrides_used=tuple(used),
    )


# ── per-run activation ───────────────────────────────────────────────────────
_active: contextvars.ContextVar[Budgets | None] = contextvars.ContextVar(
    "hivemind_budgets", default=None)


def set_active(b: Budgets) -> None:
    """Call once per run (duo run start / slot load). Handler code reads
    active() — never a hardcoded limit."""
    _active.set(b)


def active() -> Budgets:
    """The active run budgets, or a 16k fallback resolve for contexts without
    a run (direct chat, engine start). The fallback reproduces the tuned
    pre-budget behavior at the 16k base case."""
    b = _active.get()
    if b is not None:
        return b
    global _fallback
    if _fallback is None:
        _fallback = resolve(16384, CPT_DEFAULT, 4096, {})
    return _fallback


_fallback: Budgets | None = None


# ── chars-per-token EMA (per model, live) ────────────────────────────────────
_EMA_ALPHA = 0.2
_cpt_ema: dict[str, float] = {}


def update_cpt_ema(model: str, sent_chars: int, prompt_tokens: int) -> float | None:
    """Feed one real measurement (sent message chars vs server prompt_tokens).
    Returns the new EMA value, or None when the sample was unusable."""
    try:
        if not prompt_tokens or prompt_tokens <= 0 or sent_chars <= 0:
            return None
        ratio = sent_chars / float(prompt_tokens)
        if not (1.5 < ratio < 12.0):   # implausible sample — ignore
            return None
        key = str(model or "unknown")
        old = _cpt_ema.get(key, CPT_DEFAULT)
        new = round(old * (1 - _EMA_ALPHA) + ratio * _EMA_ALPHA, 3)
        _cpt_ema[key] = new
        return new
    except Exception:
        return None


def cpt_for(model: str) -> float:
    return _cpt_ema.get(str(model or ""), CPT_DEFAULT)
