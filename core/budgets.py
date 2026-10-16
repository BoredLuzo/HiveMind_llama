"""Budgets — one resolver per run, derived sizes only (2026-10-14).

Design principle: nothing branches on model classes, everything is
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
import json
from dataclasses import dataclass, asdict
from pathlib import Path

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
    PREFILL_RESULT_MAX_SECONDS, PREFILL_TPS_DEFAULT,
    PREFILL_MIN_PROMPT_TOKENS,
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
    prefill_tps: float
    # provenance
    overrides_used: tuple

    def as_dict(self) -> dict:
        return asdict(self)


def resolve(n_ctx: int, cpt: float | None = None, max_out: int = 4096,
            settings: dict | None = None,
            prefill_tps: float | None = None) -> Budgets:
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
    # PREFILL-AWARE CAPS (2026-10-14, task 4): a tool result is prefilled as
    # the tail of the next round — its own chars / cpt / tps is the worst-case
    # prefill TIME it costs. Freeze rule: `prefill_tps` is the PERSISTED EMA
    # from previous runs, resolved once per run; live updates only feed the
    # next run. tps=20 caps 16k/3.92 at ~4.7k chars (vs the 7.7k fraction —
    # on fast slots the fraction stays the binding dial).
    tps = float(prefill_tps) if prefill_tps and prefill_tps > 1 else PREFILL_TPS_DEFAULT
    prefill_time_cap = max(1200, int(PREFILL_RESULT_MAX_SECONDS * tps * cpt))
    tool_result_cap = min(tool_result_cap, prefill_time_cap)
    injection_budget = min(injection_budget, prefill_time_cap)
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
        injection_msg_cap=max(300, min(4000, injection_budget // 3)),
        anchor_lines=anchor_lines,
        outline_items=outline_items,
        compress_trigger_tokens=compress_trigger,
        compress_rest_tokens=compress_rest,
        growth_tokens=growth,
        prefill_tps=round(tps, 2),
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


def schema_budget_for(n_ctx: int, model: str = "", max_out: int = 1200,
                      settings: dict | None = None) -> int:
    """Schema budget (chars) for a path that resolves no full run budgets -
    the direct tiers, the tool agent and the helper agents. SCHEMA_FRAC of the
    context, in the model's measured chars-per-token, so those paths fit their
    tool descriptions to the window instead of shipping the full set."""
    try:
        return int(resolve(int(n_ctx or 8192), cpt_for(model),
                           int(max_out or 1200), settings or {}).schema_budget)
    except (TypeError, ValueError, AttributeError, KeyError):
        return 0


# ── chars/token from a REAL tokenizer probe (2026-10-17) ────────────────────
# Until now a run used the per-model EMA (seeded with the 3.92 module default,
# from ONE live probe on one model) or that constant. When a slot for the exec
# model is already loaded, the run can ask the server itself: POST /tokenize on
# the exact tool-schema JSON returns the true token count, and chars/tokens is
# this model's real chars-per-token - no staffel, no assumption.
CPT_PROBE_MIN_CHARS = 200      # below this the per-call overhead dominates
CPT_PROBE_MIN = 1.5            # same plausibility window as the EMA sampler
CPT_PROBE_MAX = 12.0


def cpt_from_measurement(sent_chars: int, prompt_tokens: int) -> float | None:
    """chars-per-token from one real tokenizer measurement, or None if the
    sample is unusable. Mirrors update_cpt_ema's window so both paths agree."""
    try:
        if not sent_chars or not prompt_tokens:
            return None
        if sent_chars < CPT_PROBE_MIN_CHARS or prompt_tokens <= 0:
            return None
        ratio = float(sent_chars) / float(prompt_tokens)
        if not (CPT_PROBE_MIN < ratio < CPT_PROBE_MAX):
            return None
        return round(ratio, 3)
    except (TypeError, ValueError):
        return None


# ── prefill-rate EMA (per model, persisted; task 4) ──────────────────────────
# Feed from real responses: tps = prompt_n / (prompt_ms / 1000). Stored next
# to the telemetry rows; Budgets.resolve takes the PERSISTED value (freeze
# rule) - a mid-run update only affects the NEXT run.
# FIRST MEASUREMENT REPLACES THE DEFAULT (2026-10-14, task 2 review): the
# default tps is a placeholder valid only until the first stored sample -
# blending it in at alpha=0.2 would keep the placeholder at 80% weight.
_PREFILL_STATE = (Path(__file__).resolve().parent.parent / "logs" / "telemetry"
                  / "prefill_ema.json")
_PREFILL_ALPHA = 0.2


def _load_prefill_state() -> dict:
    try:
        return json.loads(_PREFILL_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def update_prefill_ema(model: str, tps: float, prompt_n: int = 0) -> float | None:
    try:
        # MIN-PROMPT FLOOR (2026-10-15, task 2b): readings from prompts below
        # PREFILL_MIN_PROMPT_TOKENS are overhead-dominated noise - ignore them
        # entirely (they neither replace the default nor enter the EMA).
        if int(prompt_n or 0) < PREFILL_MIN_PROMPT_TOKENS:
            return None
        if not tps or tps <= 0.5 or tps > 5000:
            return None
        state = _load_prefill_state()
        key = str(model or "unknown")
        if key in state:
            old = float(state[key])
            new = round(old * (1 - _PREFILL_ALPHA) + float(tps) * _PREFILL_ALPHA, 2)
        else:
            new = round(float(tps), 2)  # first stored measurement replaces the default
        state[key] = new
        _PREFILL_STATE.parent.mkdir(parents=True, exist_ok=True)
        _PREFILL_STATE.write_text(json.dumps(state), encoding="utf-8")
        return new
    except Exception:
        return None


def prefill_tps_for(model: str) -> float:
    try:
        v = float(_load_prefill_state().get(str(model or ""), 0) or 0)
        return v if v > 1 else PREFILL_TPS_DEFAULT
    except Exception:
        return PREFILL_TPS_DEFAULT
