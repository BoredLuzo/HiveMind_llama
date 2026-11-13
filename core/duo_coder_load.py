"""Duo coder load helpers (2026-11-05 split 2.7).

The coder tool-loop model-load block as module-level seams (moved
verbatim from duo_runner.py, where they had already been extracted from
run_code_duo): the coder port cache (ctx-mismatch invalidation +
3-attempt ensure_loaded reload), the actual-slot-ctx query with the
ctx-floor guard inputs, and the tool-budget resolution. The runner
delegates to these before each tool round; the MODEL-LOAD-FAIL chunk
stop (`could not be loaded`) stays in the runner.
"""
from __future__ import annotations

import asyncio
import dataclasses
import httpx
import json
import logging

from tools.definitions import _get_inline_tools
from core.duo_helpers import _resolve_tool_budget

logger = logging.getLogger("hivemind.duo")


@dataclasses.dataclass
class _DuoCoderPortCacheState:
    """Coder port-cache in/out locals (moved verbatim from run_code_duo)."""
    dport: int | None = None
    cached_coder_port: int | None = None
    cached_coder_port_ctx: int | None = None
async def _duo_resolve_coder_port_cache(
    ctx, ps: _DuoCoderPortCacheState, *,
    exec_mdl, _dtool_opts, _img_plan, _always_vision, _lsm3,
):
    # CODE-MOTION (duo-runner-split): the coder port cache fast path
    # (P1-3), the ctx-mismatch invalidation and the 3-attempt
    # ensure_loaded reload moved VERBATIM from run_code_duo's coder
    # tool-loop try; only the unpack/pack wrapper around the body is new.
    # No logic changes. The MODEL-LOAD-FAIL stop (which breaks the chunk
    # loop) stays in the runner.
    _dport = ps.dport
    _cached_coder_port = ps.cached_coder_port
    _cached_coder_port_ctx = ps.cached_coder_port_ctx
    try:
        # P1-3 FIX: Cache coder port — coder is pinned (keep_alive=-1), so
        # ensure_loaded is a no-op after the first call but still costs
        # ~200-300ms per /health re-check on Vulkan.
        if _cached_coder_port is not None:
            _target_ctx = _dtool_opts.get("num_ctx", 4096)
            if _cached_coder_port_ctx is not None and _cached_coder_port_ctx != _target_ctx:
                logger.info(
                    "[DUO] Cached port ctx mismatch (cached=%s, needed=%s) — force reload",
                    _cached_coder_port_ctx, _target_ctx,
                )
                _cached_coder_port = None
                _cached_coder_port_ctx = None
                # RECLAIM-STALL-FIX (2026-09-06): this waited in a
                # wait_for_vram_reclaim for needed(full ctx)+768 —
                # structurally unreachable while the old slot is still
                # loaded (the kill only happens in the ensure_loaded
                # ctx-mismatch branch below) → guaranteed 45s stall.
                # The manager kill + margin resolution (768→256→error)
                # now free VRAM correctly.
                logger.info(
                    "[DUO] Cached port ctx mismatch — reload via ensure_loaded "
                    "(ctx-mismatch kill + margin resolution in the manager)"
                )
            else:
                _dport = _cached_coder_port
                # Touch keep-alive so idle monitor doesn't kill model mid-run
                try:
                    await _lsm3.touch_if_loaded(exec_mdl)
                except Exception as _err:
                    if isinstance(_err, (
                        GeneratorExit,
                        asyncio.CancelledError,
                        KeyboardInterrupt,
                        SystemExit,
                    )):
                        raise
                    logger.debug(
                        "[DUO] Suppressed error at keep-alive touch: %s",
                        _err, exc_info=True
                    )
        # MISMATCH-RELOAD-FIX (2026-09-10): the ctx-mismatch branch
        # above only cleared the cached port — the ensure_loaded call
        # lived in the `else` of the cache check, so a mismatch fell
        # straight into `_dport is None` → _loop_detected stop with 0
        # tool rounds. `_dport` still None (mismatch or empty cache)
        # now always runs the reload.
        if _dport is None:
            for _connect_attempt in range(3):
                try:
                    # VISION RELOAD (2026-10-04): keep the mmproj
                    # when the image plan routes raw images to the
                    # coder - the cached slot may predate the plan
                    _dport = await _lsm3.ensure_loaded(exec_mdl, num_ctx=_dtool_opts.get("num_ctx", 4096), n_parallel=1, ctx_graceful=False, pin=True,
                                                        vision=(_img_plan.get("coder") == "raw" or _always_vision))
                    break
                except Exception as _ce:
                    if _connect_attempt < 2:
                        yield await ctx.emit({"type": "status",
                            "content": f"⟳ Model unreachable, attempt {_connect_attempt+2}/3 in 3s…"})
                        await asyncio.sleep(3.0)
                    else:
                        _dport = None
                        logger.warning("[DUO] ensure_loaded for %s failed after 3 attempts: %s",
                                       exec_mdl, _ce)
            _cached_coder_port = _dport
            _cached_coder_port_ctx = _dtool_opts.get("num_ctx", 4096)
    finally:
        ps.dport = _dport
        ps.cached_coder_port = _cached_coder_port
        ps.cached_coder_port_ctx = _cached_coder_port_ctx
@dataclasses.dataclass
class _DuoSlotCtxState:
    """Slot-ctx query in/out locals (moved verbatim from run_code_duo)."""
    dtool_ctx: int = 0
    cached_coder_port_ctx: int | None = None
    actual_ctx: int = 0
    slot_degraded: bool = False
    slot_ctx_floor_stop: bool = False
    dtool_ctx_requested: int = 0
async def _duo_query_actual_slot_ctx(
    ps: _DuoSlotCtxState, *,
    _lsm3, _dport, _dtool_opts, _duo_coder_tok,
):
    # CODE-MOTION (duo-runner-split): the CTX-ACTUAL slot query (manager
    # slots, /props fallback, guard/clamp/budget follow-the-slot) and the
    # Guard-C floor-precondition flags moved VERBATIM from run_code_duo's
    # coder tool-loop try; only the unpack/pack wrapper around the body is
    # new. No logic changes.
    _dtool_ctx = ps.dtool_ctx
    _cached_coder_port_ctx = ps.cached_coder_port_ctx
    _actual_ctx = ps.actual_ctx
    _slot_degraded = ps.slot_degraded
    _slot_ctx_floor_stop = ps.slot_ctx_floor_stop
    _dtool_ctx_requested = ps.dtool_ctx_requested
    try:
        # CTX-ACTUAL (2026-09-06): the manager can downscale the slot
        # (PRE-FLIGHT ctx-down when VRAM is tight), so the server runs a
        # SMALLER n_ctx than configured. Guard/threshold/clamp/max_tokens
        # must follow the REAL slot ctx - otherwise prompt+budget exceed
        # the slot (llama HTTP 400) and the run churns through
        # force-compressions every few rounds.
        _dtool_ctx_requested = int(_dtool_ctx)
        _actual_ctx = 0
        try:
            for _s_act in _lsm3._slots:
                if getattr(_s_act, "port", None) == _dport and getattr(_s_act, "_num_ctx", 0):
                    _actual_ctx = int(_s_act._num_ctx)
                    break
            if not _actual_ctx:
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(connect=3.0, read=3.0, write=3.0, pool=3.0),
                    ) as _c_act:
                        _props_act = (await _c_act.get(
                            f"http://127.0.0.1:{_dport}/props"
                        )).json()
                    _actual_ctx = int((_props_act.get("default_generation_settings") or {}).get("n_ctx") or 0)
                except Exception:
                    pass
            if _actual_ctx > 0 and _actual_ctx < _dtool_ctx_requested:
                logger.warning(
                    "[CTX-ACTUAL] slot ctx=%d < configured %d - guard/clamp/budget follow the slot",
                    _actual_ctx, _dtool_ctx_requested,
                )
                _dtool_ctx = _actual_ctx
                _dtool_opts["num_ctx"] = _actual_ctx
                _dtool_opts["num_predict"] = min(
                    max(int(_duo_coder_tok), 1024), _actual_ctx
                )
                _cached_coder_port_ctx = _actual_ctx
        except Exception as _ctx_act_err:
            logger.debug("[CTX-ACTUAL] query failed: %s", _ctx_act_err)
        # CTX-FLOOR-PRECONDITION (Guard C): slot runs smaller than
        # requested by the coder (manager degradation) — stored for
        # the early round-loop stop instead of compress churn (paths 1/2).
        _slot_degraded = bool(
            _actual_ctx > 0
            and _actual_ctx < _dtool_ctx_requested
        )
        _slot_ctx_floor_stop = False  # Guard-C telemetry (see LOOP-DETECT-STOP)
    finally:
        ps.dtool_ctx = _dtool_ctx
        ps.cached_coder_port_ctx = _cached_coder_port_ctx
        ps.actual_ctx = _actual_ctx
        ps.slot_degraded = _slot_degraded
        ps.slot_ctx_floor_stop = _slot_ctx_floor_stop
        ps.dtool_ctx_requested = _dtool_ctx_requested
@dataclasses.dataclass
class _DuoToolBudgetsState:
    """Tool-budget resolve in/out locals (moved verbatim from run_code_duo)."""
    xtools_ws: list = dataclasses.field(default_factory=list)
    compress_threshold: int = 0
    max_tool_rounds: int = 0
    max_tool_rounds_cfg: int = 0
async def _duo_resolve_tool_budgets(
    ctx, state, ps: _DuoToolBudgetsState, *,
    exec_mdl, _dtool_ctx, _duo_out_budget, _duo_ws, _stored_threshold,
    duo_rounds, _duo_runtime_profile, _accumulated_replan_bonus,
    _cached_coder_port,
):
    # CODE-MOTION (duo-runner-split): the BUDGETS resolve (REAL-CPT probe,
    # core.budgets.resolve + set_active, tool-schema rebuild from the
    # schema budget, telemetry) and the tool-round budget derivation
    # (headroom compress threshold + _resolve_tool_budget) moved VERBATIM
    # from run_code_duo's coder tool-loop try; only the unpack/pack
    # wrapper around the body is new. No logic changes.
    _xtools_ws = ps.xtools_ws
    _compress_threshold = ps.compress_threshold
    _max_tool_rounds = ps.max_tool_rounds
    _max_tool_rounds_cfg = ps.max_tool_rounds_cfg
    try:
        # BUDGETS (2026-10-14, redesign of audit phase 6):
        # ONE resolve per run from measured sizes only — n_ctx,
        # the per-model chars/token EMA and the output budget.
        # Nothing here branches on model names; settings keys
        # "budget_<field>" override individual derived values
        # (None = auto). Frozen for the whole run: everything the
        # schema shows (tool caps, tool list) must not change
        # mid-run or the prefix cache dies.
        from core import budgets as _budg
        from backend.llama_client import tokenize_count
        # REAL-CPT PROBE (2026-10-17): if the coder slot is already
        # up, ask the server for the true token count of the exact
        # tool-schema JSON we are about to send. chars/tokens is
        # then THIS model's measured ratio instead of the module
        # default 3.92 / the previous runs' EMA. Failure -> EMA.
        _cpt_for_run = _budg.cpt_for(exec_mdl)
        _cpt_source = "ema"
        try:
            _probe_port = int(_cached_coder_port or 0)
            if _probe_port and _xtools_ws:
                _probe_txt = json.dumps(_xtools_ws)
                _probe_toks = await tokenize_count(_probe_port, _probe_txt)
                _probe_cpt = _budg.cpt_from_measurement(
                    len(_probe_txt), int(_probe_toks or 0))
                if _probe_cpt:
                    _cpt_for_run = _probe_cpt
                    _cpt_source = f"tokenize({len(_probe_txt)}c/{_probe_toks}t)"
        except (TypeError, ValueError, KeyError, AttributeError) as _cpt_err:
            logger.debug("[BUDGETS] cpt probe skipped: %s", _cpt_err)
        _run_budgets = _budg.resolve(
            n_ctx=int(_dtool_ctx or 16384),
            cpt=_cpt_for_run,
            max_out=int(_duo_out_budget or 1024),
            settings=ctx.settings,
            # FREEZE RULE (task 4): the persisted EMA from
            # PREVIOUS runs - never a mid-run value.
            prefill_tps=_budg.prefill_tps_for(exec_mdl),
        )
        _budg.set_active(_run_budgets)
        state["_budgets"] = _run_budgets
        from core.duo_helpers import resolve_coder_tool_mode as _resolve_tier_mode
        _xtools_ws = _get_inline_tools(
            include_websearch=_duo_ws,
            mode=_resolve_tier_mode(ctx.settings),
            budget_chars=_run_budgets.schema_budget,
            # Telegram runs may deliver files the model creates; desktop and
            # API runs never see a channel they cannot use.
            include_send_file=(str(ctx.settings.get("source") or "").startswith("telegram")),
            settings=ctx.settings)
        state["_xtools_ws"] = _xtools_ws
        logger.info(
            "[BUDGETS] ctx=%d cpt=%.2f(%s) out=%d -> tool_cap=%d read_cap=%d schema=%d "
            "inj=%d anchor=+%d outline=%d compress@%d/rest@%d overrides=%s",
            _run_budgets.n_ctx, _run_budgets.cpt, _cpt_source,
            _run_budgets.max_out,
            _run_budgets.tool_result_cap, _run_budgets.read_char_cap,
            _run_budgets.schema_budget, _run_budgets.injection_budget,
            _run_budgets.anchor_lines, _run_budgets.outline_items,
            _run_budgets.compress_trigger_tokens,
            _run_budgets.compress_rest_tokens,
            ",".join(_run_budgets.overrides_used) or "-",
        )
        try:
            from core import telemetry as _b_tel
            _b_tel.append_round({
                "phase": "budgets",
                "model": exec_mdl,
                **_run_budgets.as_dict(),
            })
        except Exception:
            pass
        _max_tool_rounds_cfg = int(ctx.settings.get("duo_max_tool_rounds", 64))
        _max_tool_rounds_cfg = 8 if duo_rounds > 1 else _max_tool_rounds_cfg
        if not _stored_threshold:
            # BUDGETS (2026-10-14): headroom-based compression
            # trigger (free < 1.5 × reserve with reserve =
            # clamped max_out + growth) replaces the fixed floor —
            # write-heavy runs trigger earlier without a special
            # case. An explicit ui_threshold still wins.
            _compress_threshold = _run_budgets.compress_trigger_tokens
        _max_tool_rounds = _resolve_tool_budget(
            _max_tool_rounds_cfg,
            ctx.duo_config.until_finished,
            settings=ctx.settings,
            profile=_duo_runtime_profile,
        ) + _accumulated_replan_bonus
    finally:
        ps.xtools_ws = _xtools_ws
        ps.compress_threshold = _compress_threshold
        ps.max_tool_rounds = _max_tool_rounds
        ps.max_tool_rounds_cfg = _max_tool_rounds_cfg
