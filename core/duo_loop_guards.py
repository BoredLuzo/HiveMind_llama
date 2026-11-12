"""Duo loop guards (W4a, 2026-11-07 maintainability sprint).

Health check every 10th tool round, static repo-map pinning, calibrated
token estimate and GUARD C (ctx-floor stop). Moved VERBATIM out of the
tool-round loop; `ps.action == "break"` ends the loop, all outputs sync
in `finally`.
"""
from __future__ import annotations

import dataclasses
import logging

import httpx

from core.duo.preflight import (
    calib_est_tokens as _calib_tokens,
    _ld_setter,
)
from utils.token import estimate_ctx_tokens as _estimate_ctx_tokens

logger = logging.getLogger("hivemind.duo")


@dataclasses.dataclass
class _DuoLoopGuardsState:
    exec_mdl: str = ""
    actual_ctx: int = 0
    dtool_ctx_requested: int = 0
    slot_degraded: bool = False
    total_tool_rounds: int = 0
    dtool_msgs: list = dataclasses.field(default_factory=list)
    dtool_opts: dict = dataclasses.field(default_factory=dict)
    coder_real_prompt_tokens: list = dataclasses.field(default_factory=lambda: [0])
    coder_est_at_real: list = dataclasses.field(default_factory=lambda: [0])
    coder_real_at_msgs_len: list = dataclasses.field(default_factory=lambda: [0])
    dport: int | None = None
    cached_coder_port: int | None = None
    cached_coder_port_ctx: int | None = None
    est_tokens: int = 0
    guard_tokens: int = 0
    loop_detected: bool = False
    slot_ctx_floor_stop: bool = False
    action: str = "proceed"   # "break" ends the tool-round loop


async def _duo_loop_guards(ctx, ps: _DuoLoopGuardsState):
    exec_mdl = ps.exec_mdl
    _actual_ctx = ps.actual_ctx
    _dtool_ctx_requested = ps.dtool_ctx_requested
    _slot_degraded = ps.slot_degraded
    _total_tool_rounds = ps.total_tool_rounds
    _dtool_msgs = ps.dtool_msgs
    _dtool_opts = ps.dtool_opts
    _coder_real_prompt_tokens = ps.coder_real_prompt_tokens
    _coder_est_at_real = ps.coder_est_at_real
    _coder_real_at_msgs_len = ps.coder_real_at_msgs_len
    _dport = ps.dport
    _cached_coder_port = ps.cached_coder_port
    _cached_coder_port_ctx = ps.cached_coder_port_ctx
    _est_tokens = ps.est_tokens
    _guard_tokens = ps.guard_tokens
    _loop_detected = ps.loop_detected
    _slot_ctx_floor_stop = ps.slot_ctx_floor_stop
    try:
        if _total_tool_rounds > 0 and _total_tool_rounds % 10 == 0:
            _hc_ok = False
            try:
                _hc_url = f"http://127.0.0.1:{_dport}/v1/models"
                async with httpx.AsyncClient(timeout=httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)) as _hc_client:
                    _hc_r = await _hc_client.get(_hc_url)
                    _hc_ok = _hc_r.status_code < 500
            except Exception:
                pass
            if not _hc_ok:
                logger.warning("[HEALTH-CHECK] llama-server port %s unreachable - trying reload", _dport)
                try:
                    from backend.llama_server_manager import manager as _lsm_hc
                    await _lsm_hc.evict(exec_mdl)
                    _dport = await _lsm_hc.ensure_loaded(
                        exec_mdl, num_ctx=_dtool_opts.get("num_ctx", 4096),
                        n_parallel=1, ctx_graceful=False, pin=True,
                        vision=bool(_dtool_opts.get("vision", False)))
                    _cached_coder_port = _dport
                    _cached_coder_port_ctx = _dtool_opts.get("num_ctx", 4096)  # CTX-GUARD
                    yield await ctx.emit({"type": "status",
                        "content": "🩺 Health check: server restarted."})
                except Exception as _hc_exc:
                    logger.error("[HEALTH-CHECK] Server reload failed: %s", _hc_exc)
                    _ld_setter(2716); _loop_detected = True
                    yield await ctx.emit({"type": "status",
                        "content": f"⛔ Server dead after health check (port {_dport}) — run aborted."})
                    ps.action = "break"
                    return
        # REPO-MAP-PIN (2026-09-06): static repo-map into the pinned
        # system message once per built list (marker makes later
        # rounds a no-op). Extends the cache-stable prefix beyond the
        # ~7.1k system/tool base; the map is no longer inside the big
        # first user message that full compression rewrites.
        if ctx.settings.get("duo_pin_static_map", True):
            try:
                from core.repomap_pin import pin_static_map as _pin_map_once
                if _pin_map_once(_dtool_msgs):
                    logger.warning(
                        "[REPO-MAP-PIN] static repo-map moved into pinned system message"
                    )
            except Exception as _pin_err:
                logger.debug("[REPO-MAP-PIN] skipped: %s", _pin_err)
        _est_tokens = int(_calib_tokens(_coder_real_prompt_tokens, _coder_est_at_real, int(_estimate_ctx_tokens(_dtool_msgs))))
        # GUARD-REAL-BASIS (2026-08-26): all guard decisions on the
        # (evict/compress/90%/72%/notices) use the real
        # prompt_tokens value of the last round, fallback to the
        # estimator (round 1 / backend without usage_meta).
        _guard_tokens = int(_coder_real_prompt_tokens[0] or _est_tokens)
        # DELTA-ON-REAL (2026-10-14, audit phase 2): the server
        # value is one round stale. Count what was appended
        # since that POST (tool results, injections) with the
        # real/est ratio, so a full big-write round cannot slip
        # past the compression trigger.
        if _coder_real_prompt_tokens[0] > 0 and len(_dtool_msgs) > _coder_real_at_msgs_len[0]:
            try:
                _r2e = _coder_real_prompt_tokens[0] / max(1, _coder_est_at_real[0])
                _delta_chars = sum(
                    len(str(_m.get("content", "") or ""))
                    + len(str(_m.get("tool_calls", "") or ""))
                    for _m in _dtool_msgs[_coder_real_at_msgs_len[0]:]
                )
                _guard_tokens = int(_guard_tokens + _delta_chars / 4.0 * _r2e)
            except Exception:
                pass
        # ── GUARD C: CTX-FLOOR-STOP (2026-09-06) ─────────────
        # If the slot is degraded (real slot-ctx < requested ctx)
        # and the round-1 baseline (pinned sys/repo-map/plan/goal)
        # already exhausts the usable space, compression is
        # pointless — the run used to churn for minutes in path 1
        # ([CTX-FULL] skip×4) or path 2 (compress-streak×3) and
        # then stopped unspecifically. Guard C fires BEFORE the
        # first tool round with a clear message and never lets paths 1/2
        # be reached in exactly this case (no double firing:
        # same exit funnel as all _ld_setter stops).
        if _slot_degraded and _total_tool_rounds <= 0:
            try:
                from backend.llama_manager_utils import CTX_DOWN_MIN as _CTX_FLOOR_MIN
            except Exception:
                _CTX_FLOOR_MIN = 8192
            _usable_floor = _actual_ctx - 512
            # IMPORTANT: _est_tokens (fresh estimate of the CURRENT
            # _dtool_msgs) instead of _guard_tokens — the latter can carry
            # the previous round's cached real-prompt-tokens value
            # (false-positive stop after compression/chunk switch).
            if _actual_ctx < _CTX_FLOOR_MIN and _est_tokens >= _usable_floor:
                _slot_ctx_floor_stop = True
                logger.warning(
                    "[CTX-FLOOR-STOP] slot ctx=%d < min %d; round-1 est=%d >= usable %d — "
                    "aborting instead of compress churn (path 1/2 skipped)",
                    _actual_ctx, _CTX_FLOOR_MIN, int(_est_tokens), _usable_floor,
                )
                yield await ctx.emit({
                    "type": "status",
                    "content": (
                        f"⛔ Coder slot running at ctx={_actual_ctx} only "
                        f"(requested: {_dtool_ctx_requested}, minimum {_CTX_FLOOR_MIN}) — "
                        f"round-1 context (~{int(_est_tokens)} tokens) does not fit. "
                        f"Close other GPU users, lower the coder ctx or pick a smaller model."
                    ),
                })
                _ld_setter(3246); _loop_detected = True
                ps.action = "break"
                return
    finally:
        ps.dport = _dport
        ps.cached_coder_port = _cached_coder_port
        ps.cached_coder_port_ctx = _cached_coder_port_ctx
        ps.est_tokens = _est_tokens
        ps.guard_tokens = _guard_tokens
        ps.loop_detected = _loop_detected
        ps.slot_ctx_floor_stop = _slot_ctx_floor_stop
