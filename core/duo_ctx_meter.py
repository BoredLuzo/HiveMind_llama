"""Ctx-meter + SWA warning round (F13, 2026-11-08).

The per-round context meter, sliding-window (SWA) reprefill warning and the
60%/85% runtime notices were the last pure-emit block inside the tool loop.
Moved verbatim into a small result object; the runner emits the events and
appends the messages in order. No loop control.

The ctx-shrink round (F13, 2026-11-08) lives here too: the emergency-evict
plus mask-before-compress decision block of the tool loop, moved verbatim
into a small result object (`_CtxShrinkResult`). The runner emits the
collected status events and keeps the LLM compression cycle.
"""
from __future__ import annotations

import dataclasses
import logging

from backend.llama_server_manager import manager as _lsm
from context.compression import _evict_stale_tool_outputs
from context.ctx_guard import (
    decide_context_action as _decide_context_action,
    DEFAULT_EVICT_TARGET_PCT as _EVICT_TARGET_PCT,
    DEFAULT_EVICT_HARD_FLOOR_PCT as _EVICT_HARD_FLOOR_PCT,
)
from core.duo.preflight import calib_est_tokens as _calib_tokens
from utils.token import estimate_ctx_tokens as _estimate_ctx_tokens

logger = logging.getLogger("hivemind.duo")


@dataclasses.dataclass
class _CtxMeterResult:
    ctx_pressure_peak: float = 0.0
    swa_triggered_compress: bool = False
    force_compress_next: bool = False
    ctx_critical_warned: bool = False
    ctx_pressure_warned: bool = False
    events: list = dataclasses.field(default_factory=list)
    messages: list = dataclasses.field(default_factory=list)


def _ctx_meter_round(
    *, dtool_ctx, dport, est_tokens, real_prompt_tokens, real_cached_tokens,
    guard_tokens, ctx_compressions, max_compressions,
    ctx_pressure_peak, swa_triggered_compress, force_compress_next,
    ctx_critical_warned, ctx_pressure_warned,
) -> _CtxMeterResult:
    """Build the ctx_meter/SWA events and the 60%/85% runtime notices."""
    res = _CtxMeterResult(
        ctx_pressure_peak=ctx_pressure_peak,
        swa_triggered_compress=swa_triggered_compress,
        force_compress_next=force_compress_next,
        ctx_critical_warned=ctx_critical_warned,
        ctx_pressure_warned=ctx_pressure_warned,
    )
    if dtool_ctx > 0:
        res.ctx_pressure_peak = max(
            float(ctx_pressure_peak), float(est_tokens) / float(dtool_ctx))
        _swa_win = 0
        try:
            for _s in _lsm._slots:
                if _s.port == dport and _s.swa_window:
                    _swa_win = _s.swa_window
                    break
        except (AttributeError, TypeError, RuntimeError, OSError):
            pass
        _swa_warn = bool(_swa_win and est_tokens > _swa_win * 0.85)
        res.events.append({
            "type": "ctx_meter",
            "est_tokens": int(real_prompt_tokens or est_tokens),
            "real_tokens": int(real_prompt_tokens),
            "ctx_limit": int(dtool_ctx),
            "cached_tokens": int(real_cached_tokens),
            "compressing": False,
            "swa_window": _swa_win,
            "swa_warning": _swa_warn,
        })
        if _swa_warn:
            res.events.append({
                "type": "system_warning",
                "code": "swa_reprefill_zone",
                "message": (f"Context ({int(est_tokens)} tok) approaching SWA "
                            f"window ({_swa_win} tok) — reprefill slowdown expected"),
            })
            if not res.swa_triggered_compress:
                res.swa_triggered_compress = True
                if ctx_compressions < max_compressions:
                    res.force_compress_next = True
    _ctx_pct = int((guard_tokens / dtool_ctx) * 100) if dtool_ctx > 0 else 0
    if _ctx_pct >= 85 and not res.ctx_critical_warned:
        res.messages.append({
            "role": "user",
            "content": (
                "[RUNTIME NOTICE] "
                f"[CTX CRITICAL: ~{_ctx_pct}% full] Stop reading new files. "
                "Complete current edits and call task_complete."
            ),
        })
        res.ctx_critical_warned = True
    elif _ctx_pct >= 60 and not res.ctx_pressure_warned:
        res.messages.append({
            "role": "user",
            "content": (
                "[RUNTIME NOTICE] "
                f"[CTX: ~{_ctx_pct}% full] Avoid large reads. "
                "Prefer targeted read_file(start_line/end_line) "
                "and search_code over full-file reads."
            ),
        })
        res.ctx_pressure_warned = True
    return res


@dataclasses.dataclass
class _CtxShrinkResult:
    compress_ok: bool = False
    guard_decision: object = None
    est_tokens: int = 0
    guard_tokens: int = 0
    masked_n: int = 0
    events: list = dataclasses.field(default_factory=list)


def _ctx_shrink_round(
    *, dtool_ctx, compress_threshold, est_tokens, guard_tokens,
    can_compress, force_compress_next, total_tool_rounds, dtool_msgs,
    tool_ctx_lru, coder_real_prompt_tokens, coder_est_at_real,
    recent_focus_paths, mask_enabled,
) -> _CtxShrinkResult:
    """Emergency-evict + mask-before-compress decision, moved verbatim.

    Mutates `dtool_msgs` (in-place eviction) and the by-ref lists
    `coder_real_prompt_tokens` / `coder_est_at_real` (stale-real reset)
    exactly like the inline block did; the runner emits `events` in order
    and keeps the compression cycle.
    """
    res = _CtxShrinkResult(est_tokens=int(est_tokens), guard_tokens=int(guard_tokens))
    # CACHE-FRIENDLY (2026-09-04): compression is the
    # primary shrink. In-place semantic eviction is ONLY
    # the emergency valve when no compression is left
    # (cap reached) AND the context is >90% full. Between
    # two compressions the history stays append-only ->
    # the llama.cpp prefix cache stays alive and rounds
    # are fast (tail prefill only).
    # LEGACY-REMOVAL (2026-09-09): the in-place-eviction A/B branch
    # (duo_cache_friendly_ctx=False) is gone; compression-first is the
    # only regime.
    res.guard_decision = _decide_context_action(
        guard_tokens=int(res.guard_tokens),
        ctx_tokens=int(dtool_ctx),
        threshold=int(compress_threshold),
        can_compress=can_compress,
        force_compress=bool(force_compress_next),
    )
    res.compress_ok = (res.guard_decision.action == "compress")
    # First-round protection: the initial context
    # (explore/static map) may only be compressed via
    # threshold after >=1 tool round (emergency >90%
    # still OK).
    if res.compress_ok and res.guard_decision.reason == "threshold" and total_tool_rounds <= 0:
        res.compress_ok = False
    # EMERGENCY-EVICT (bounded two-pass): the same block
    # ran byte-identically twice; each pass re-decides and
    # only a still-emergency second pass evicts again.
    for _ in range(2):
        if res.compress_ok or res.guard_decision.action != "emergency_evict":
            break
        _est_before_evict = int(res.est_tokens)
        _evicted_n = _evict_stale_tool_outputs(
            messages=dtool_msgs,
            lru=tool_ctx_lru,
            target_token_budget=int(dtool_ctx * _EVICT_TARGET_PCT),
            hard_floor_tokens=int(dtool_ctx * _EVICT_HARD_FLOOR_PCT),
            protect_paths=recent_focus_paths,
        )
        if _evicted_n > 0:
            res.est_tokens = _calib_tokens(coder_real_prompt_tokens, coder_est_at_real, int(_estimate_ctx_tokens(dtool_msgs)))
            # METER-FIX (2026-08-25): the real value is now
            # STALE (pre-eviction). Reset -> naechster
            # ctx_meter nutzt den frischen Schaetzer.
            coder_real_prompt_tokens[0] = 0
            coder_est_at_real[0] = 0
            logger.warning(
                "[CTX-EVICT-EMERGENCY] est_before=%d evicted=%d est_after=%d ctx=%d target=%d hard_floor=%d real_before=%d reason=%s",
                _est_before_evict, _evicted_n, int(res.est_tokens), int(dtool_ctx),
                int(dtool_ctx * _EVICT_TARGET_PCT), int(dtool_ctx * _EVICT_HARD_FLOOR_PCT), int(res.guard_tokens),
                res.guard_decision.reason,
            )
            res.events.append({
                "type": "status",
                "content": (
                    f"🧹 Emergency context eviction (no compression left): {_evicted_n} "
                    "stale tool output(s) replaced by recall markers."
                ),
            })
        res.guard_decision = _decide_context_action(
            guard_tokens=int(res.guard_tokens),
            ctx_tokens=int(dtool_ctx),
            threshold=int(compress_threshold),
            can_compress=can_compress,
            force_compress=bool(force_compress_next),
        )
        res.compress_ok = (res.guard_decision.action == "compress")
        # First-round protection: the initial context
        # (explore/static map) may only be compressed via
        # threshold after >=1 tool round (emergency >90%
        # still OK).
        if res.compress_ok and res.guard_decision.reason == "threshold" and total_tool_rounds <= 0:
            res.compress_ok = False
    # MASK-FIRST (2026-10-14, audit phase 4): the
    # deterministic observation masking (stale tool
    # outputs -> recall markers, same machinery as the
    # emergency valve) runs BEFORE the LLM summary
    # decision. No LLM call, no slot evict, bounded to
    # the threshold target — and it can CLEAR
    # compress_ok, so the LLM compression below only
    # fires when masking alone was not enough.
    # Settings toggle: duo_mask_before_compress.
    if res.compress_ok and mask_enabled:
        try:
            res.masked_n = _evict_stale_tool_outputs(
                messages=dtool_msgs,
                lru=tool_ctx_lru,
                target_token_budget=int(compress_threshold),
                hard_floor_tokens=int(dtool_ctx * _EVICT_HARD_FLOOR_PCT),
                protect_paths=recent_focus_paths,
            )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError,
                AttributeError, IndexError) as _mask_err:
            logger.warning("[CTX-MASK] failed: %s", _mask_err)
            res.masked_n = 0
        if res.masked_n > 0:
            res.est_tokens = _calib_tokens(coder_real_prompt_tokens, coder_est_at_real, int(_estimate_ctx_tokens(dtool_msgs)))
            res.guard_tokens = max(
                int(coder_real_prompt_tokens[0] or 0), int(res.est_tokens))
            if res.guard_tokens <= int(compress_threshold):
                res.compress_ok = False
                res.events.append({"type": "status",
                    "content": f"🧹 Masked {res.masked_n} stale tool output(s) — no summary needed."})
                logger.info(
                    "[CTX-MASK] masked=%d est_after=%d threshold=%d (LLM compression skipped)",
                    res.masked_n, int(res.est_tokens), int(compress_threshold),
                )
            else:
                res.events.append({"type": "status",
                    "content": f"🧹 Masked {res.masked_n} stale tool output(s) — running summary."})
                logger.info(
                    "[CTX-MASK] masked=%d est_after=%d guard=%d threshold=%d (LLM compression still needed)",
                    res.masked_n, int(res.est_tokens), int(res.guard_tokens), int(compress_threshold),
                )
    return res
